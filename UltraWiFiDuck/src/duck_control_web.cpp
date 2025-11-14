/*
 * Enhanced MQTT-based HID control for ESP32-S2
 * Multi-broker support, state-based keyboard protocol, optimized latency
 *
 * Key improvements:
 * - Multi-broker failover for reliability
 * - State-based keyboard protocol (resilient to packet loss)
 * - Reduced latency (MIN_HID_INTERVAL_MS = 20ms)
 * - QoS optimization (mouse=0, keyboard=1)
 * - Automatic recovery and health monitoring
 */

#include "duck_control_web.h"
#include <Arduino.h>
#include <WiFi.h>
#include <ArduinoJson.h>
#include <AsyncMqttClient.h>
#include <USB.h>
#include <USBHIDMouse.h>
#include <USBHIDKeyboard.h>
#include <esp_task_wdt.h>
#include <tusb.h>
#include <set>

static AsyncMqttClient mqttClient;
static TimerHandle_t mqttReconnectTimer;
static TimerHandle_t hidTimeoutTimer;
static USBHIDKeyboard kbd;
static USBHIDMouse Mouse;

// Multi-broker configuration
struct BrokerConfig {
    const char* host;
    uint16_t port;
};

BrokerConfig MQTT_BROKERS[] = {
    {"broker.emqx.io", 1883},
    {"test.mosquitto.org", 1883},
    // Add more brokers as needed
};

const size_t MQTT_BROKER_COUNT = sizeof(MQTT_BROKERS) / sizeof(MQTT_BROKERS[0]);
size_t currentBrokerIndex = 0;
uint8_t brokerFailureCount = 0;
const uint8_t MAX_BROKER_FAILURES = 3;  // Try each broker 3 times before rotating

const char* DEVICE_ID = "esp32_hid_001";  // Should match Python script

// Topics
String mouseTopic = "hid/" + String(DEVICE_ID) + "/mouse";
String keyTopic = "hid/" + String(DEVICE_ID) + "/key";
String statusTopic = "hid/" + String(DEVICE_ID) + "/status";
String pingTopic = "hid/" + String(DEVICE_ID) + "/ping";

// HID Constants - Optimized for lower latency
const int HID_TIMEOUT_MS = 1000;      // Inactivity timeout for auto-release
const int MIN_HID_INTERVAL_MS = 20;   // Reduced from 50ms to 20ms (~50Hz)
unsigned long lastHidTime = 0;

// State-based keyboard protocol: Track currently pressed keys
std::set<uint8_t> pressedKeys;

// HID timeout callback
static void hidTimeoutCallback(TimerHandle_t xTimer) {
    kbd.releaseAll();
    Mouse.release(MOUSE_LEFT | MOUSE_RIGHT | MOUSE_MIDDLE);
    pressedKeys.clear();
    Serial.println("[HID] Timeout: Released all keys and mouse buttons");
}

void connectToMqtt() {
    BrokerConfig& broker = MQTT_BROKERS[currentBrokerIndex];
    Serial.printf("[MQTT] Connecting to broker [%d/%d]: %s:%d\n",
                  currentBrokerIndex + 1, MQTT_BROKER_COUNT,
                  broker.host, broker.port);

    mqttClient.setServer(broker.host, broker.port);
    mqttClient.connect();
}

void onMqttConnect(bool sessionPresent) {
    Serial.println("[MQTT] ✓ Connected");
    Serial.printf("Session present: %d\n", sessionPresent);

    // Reset failure count on successful connection
    brokerFailureCount = 0;

    // Subscribe with optimized QoS levels
    // Mouse: QoS 0 (best effort, low latency)
    // Keyboard: QoS 1 (at least once, reliability)
    // Ping: QoS 1 (discovery reliability)
    mqttClient.subscribe(mouseTopic.c_str(), 0);
    mqttClient.subscribe(keyTopic.c_str(), 1);
    mqttClient.subscribe(pingTopic.c_str(), 1);

    Serial.printf("Subscribed to: mouse (QoS 0), key (QoS 1), ping (QoS 1)\n");

    // Publish online status with Last Will Testament
    JsonDocument statusDoc;
    statusDoc["status"] = "online";
    statusDoc["device_id"] = DEVICE_ID;
    statusDoc["current_broker_index"] = currentBrokerIndex;
    statusDoc["broker_host"] = MQTT_BROKERS[currentBrokerIndex].host;
    statusDoc["usb_connected"] = tud_mounted();
    statusDoc["timestamp"] = millis();

    String statusPayload;
    serializeJson(statusDoc, statusPayload);
    mqttClient.publish(statusTopic.c_str(), 1, true, statusPayload.c_str());

    // Set Last Will message for offline detection
    JsonDocument willDoc;
    willDoc["status"] = "offline";
    willDoc["device_id"] = DEVICE_ID;
    String willPayload;
    serializeJson(willDoc, willPayload);
    mqttClient.setWill(statusTopic.c_str(), 1, true, willPayload.c_str());
}

void onMqttDisconnect(AsyncMqttClientDisconnectReason reason) {
    Serial.printf("[MQTT] ✗ Disconnected (reason: %d)\n", static_cast<int>(reason));

    // Release all HID controls on disconnect to prevent stuck keys/buttons
    kbd.releaseAll();
    Mouse.release(MOUSE_LEFT | MOUSE_RIGHT | MOUSE_MIDDLE);
    pressedKeys.clear();
    Serial.println("[HID] Released all controls due to disconnect");

    if (WiFi.isConnected()) {
        brokerFailureCount++;

        // After MAX_BROKER_FAILURES, try next broker
        if (brokerFailureCount >= MAX_BROKER_FAILURES) {
            Serial.printf("[MQTT] Broker [%d] failed %d times, rotating to next broker...\n",
                          currentBrokerIndex, brokerFailureCount);
            currentBrokerIndex = (currentBrokerIndex + 1) % MQTT_BROKER_COUNT;
            brokerFailureCount = 0;
        }

        // Restart reconnect timer
        xTimerStart(mqttReconnectTimer, 0);
    }
}

void onMqttMessage(char* topic, char* payload, AsyncMqttClientMessageProperties properties, size_t len, size_t index, size_t total) {
    // Null terminate payload
    if (len < 512) {  // Safety check
        payload[len] = '\0';
    } else {
        Serial.println("[MQTT] Payload too large, truncating");
        payload[511] = '\0';
    }

    Serial.print("[MQTT] RX [");
    Serial.print(topic);
    Serial.print("]: ");
    Serial.println(payload);

    // Parse JSON payload
    JsonDocument doc;
    DeserializationError error = deserializeJson(doc, payload);

    if (error) {
        Serial.print("[JSON] Parse error: ");
        Serial.println(error.c_str());
        return;
    }

    String topicStr = String(topic);
    unsigned long now = millis();

    // ────────────────────────────────────────────────────────────
    // MOUSE TOPIC - Optimized latency, button actions unthrottled
    // ────────────────────────────────────────────────────────────
    if (topicStr == mouseTopic) {
        int dx = doc["dx"] | 0;
        int dy = doc["dy"] | 0;
        int wheel = doc["wheel"] | 0;
        String buttonStr = doc["button"] | "";
        String buttonAction = doc["button_action"] | "";

        // Clamp movement to HID valid range
        dx = max(-127, min(127, dx));
        dy = max(-127, min(127, dy));
        wheel = max(-127, min(127, wheel));

        // Handle button actions (never throttled for responsiveness)
        uint8_t button = 0;
        if (buttonStr == "left") button = MOUSE_LEFT;
        else if (buttonStr == "right") button = MOUSE_RIGHT;
        else if (buttonStr == "middle") button = MOUSE_MIDDLE;

        if (button != 0) {
            if (buttonAction == "press") {
                Mouse.press(button);
                Serial.printf("[HID] Mouse %s pressed\n", buttonStr.c_str());
            } else if (buttonAction == "release") {
                Mouse.release(button);
                Serial.printf("[HID] Mouse %s released\n", buttonStr.c_str());
            } else if (buttonAction == "release_all") {
                Mouse.release(MOUSE_LEFT | MOUSE_RIGHT | MOUSE_MIDDLE);
                Serial.println("[HID] All mouse buttons released");
            }
            // Nudge with zero move to force HID report
            Mouse.move(0, 0, 0);
        }

        // Throttle only movement (not buttons)
        if (dx != 0 || dy != 0 || wheel != 0) {
            if (now - lastHidTime >= MIN_HID_INTERVAL_MS) {
                Mouse.move(dx, dy, wheel);
                Serial.printf("[HID] Mouse moved: dx=%d dy=%d wheel=%d\n", dx, dy, wheel);
                lastHidTime = now;
            } else {
                Serial.println("[HID] Mouse movement throttled");
            }
        }
    }
    // ────────────────────────────────────────────────────────────
    // KEYBOARD TOPIC - Both event-based and state-based protocols
    // ────────────────────────────────────────────────────────────
    else if (topicStr == keyTopic) {
        String action = doc["action"];
        int keyCode = doc["key"] | 0;

        // Validate keyCode
        if (keyCode < 0 || keyCode > 255) {
            Serial.printf("[KEY] Invalid keyCode %d\n", keyCode);
            return;
        }

        // Throttle keyboard events
        if (now - lastHidTime < MIN_HID_INTERVAL_MS) {
            Serial.println("[HID] Keyboard command throttled");
            return;
        }

        // ── State-based protocol (resilient to packet loss) ──
        if (action == "state") {
            // Extract pressed keys array
            JsonArray pressedArray = doc["pressed"];
            std::set<uint8_t> newPressed;

            for (JsonVariant v : pressedArray) {
                uint8_t key = v.as<uint8_t>();
                newPressed.insert(key);
            }

            // Compute difference: release keys no longer in newPressed
            for (uint8_t key : pressedKeys) {
                if (newPressed.find(key) == newPressed.end()) {
                    kbd.release(key);
                    Serial.printf("[HID] State: Released key %d\n", key);
                }
            }

            // Press keys newly in newPressed
            for (uint8_t key : newPressed) {
                if (pressedKeys.find(key) == pressedKeys.end()) {
                    kbd.press(key);
                    Serial.printf("[HID] State: Pressed key %d\n", key);
                }
            }

            pressedKeys = newPressed;
            Serial.printf("[HID] State sync: %d keys pressed\n", pressedKeys.size());
        }
        // ── Legacy event-based protocol (backward compatible) ──
        else if (action == "press") {
            kbd.press(keyCode);
            pressedKeys.insert(keyCode);
            Serial.printf("[HID] Key pressed: %d\n", keyCode);
        } else if (action == "release") {
            kbd.release(keyCode);
            pressedKeys.erase(keyCode);
            Serial.printf("[HID] Key released: %d\n", keyCode);
        } else if (action == "release_all") {
            kbd.releaseAll();
            pressedKeys.clear();
            Serial.println("[HID] All keys released");
        }

        lastHidTime = now;
    }
    // ────────────────────────────────────────────────────────────
    // PING TOPIC - Health check and discovery
    // ────────────────────────────────────────────────────────────
    else if (topicStr == pingTopic) {
        String from = doc["from"];
        if (from == "host") {
            // Host is pinging for discovery, respond with status
            JsonDocument statusDoc;
            statusDoc["status"] = "alive";
            statusDoc["device_id"] = DEVICE_ID;
            statusDoc["current_broker_index"] = currentBrokerIndex;
            statusDoc["broker_host"] = MQTT_BROKERS[currentBrokerIndex].host;
            statusDoc["usb_connected"] = tud_mounted();
            statusDoc["pressed_keys_count"] = pressedKeys.size();
            statusDoc["uptime_ms"] = millis();
            statusDoc["free_heap"] = ESP.getFreeHeap();
            statusDoc["timestamp"] = millis();

            String payloadStr;
            serializeJson(statusDoc, payloadStr);
            mqttClient.publish(statusTopic.c_str(), 1, false, payloadStr.c_str());
            Serial.println("[PING] Responded to host discovery");
        }
    }

    // Reset HID timeout timer and watchdog on activity
    xTimerReset(hidTimeoutTimer, 0);
    esp_task_wdt_reset();
}

void duck_control_web_begin() {
    Serial.println("[DUCK] Initializing MQTT HID control...");

    // Initialize HID devices
    Mouse.begin();
    kbd.begin();
    Serial.println("[HID] Mouse and Keyboard initialized");

    // Setup MQTT reconnect timer
    mqttReconnectTimer = xTimerCreate(
        "mqttTimer",
        pdMS_TO_TICKS(2000),
        pdFALSE,
        (void*)0,
        reinterpret_cast<TimerCallbackFunction_t>(connectToMqtt)
    );

    // Setup HID timeout timer
    hidTimeoutTimer = xTimerCreate(
        "hidTimeout",
        pdMS_TO_TICKS(HID_TIMEOUT_MS),
        pdFALSE,
        (void*)0,
        hidTimeoutCallback
    );

    // Init watchdog (5s timeout, no panic)
    esp_task_wdt_init(5, false);
    esp_task_wdt_add(NULL);
    Serial.println("[WDT] Watchdog initialized");

    // Setup MQTT client callbacks
    mqttClient.onConnect(onMqttConnect);
    mqttClient.onDisconnect(onMqttDisconnect);
    mqttClient.onMessage(onMqttMessage);

    // Initial connection
    connectToMqtt();

    Serial.println("[DUCK] ✓ MQTT HID control initialized");
    Serial.printf("Multi-broker mode: %d broker(s) configured\n", MQTT_BROKER_COUNT);
    for (size_t i = 0; i < MQTT_BROKER_COUNT; i++) {
        Serial.printf("  [%d] %s:%d\n", i, MQTT_BROKERS[i].host, MQTT_BROKERS[i].port);
    }
}

void duck_control_mqtt_loop() {
    // Periodic health check
    static unsigned long lastCheck = 0;
    if (millis() - lastCheck > 5000) {
        Serial.printf("[HEALTH] Heap: %u bytes, Broker: %s, USB: %s, Keys: %d\n",
                      ESP.getFreeHeap(),
                      MQTT_BROKERS[currentBrokerIndex].host,
                      tud_mounted() ? "OK" : "DISC",
                      pressedKeys.size());
        lastCheck = millis();
    }
}
