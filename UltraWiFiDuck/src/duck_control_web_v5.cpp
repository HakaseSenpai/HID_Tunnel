/*
 * ESP32 HID Control v5.0 - Phase 2: HTTP Transport + mDNS Auto-Discovery
 *
 * New in Phase 2:
 * - HTTP client transport (long-polling) for maximum compatibility
 * - mDNS auto-discovery via UDP broadcast (port 37020)
 * - Auto-configuration of endpoints from discovery
 * - Out-of-box experience with zero manual configuration
 *
 * Transports: MQTT, WebSocket, HTTP (all with auto-discovery)
 */

#include "duck_control_web.h"
#include <Arduino.h>
#include <WiFi.h>
#include <ArduinoJson.h>
#include <AsyncMqttClient.h>
#include <WebSocketsClient.h>
#include <HTTPClient.h>
#include <WiFiUdp.h>
#include <USB.h>
#include <USBHIDMouse.h>
#include <USBHIDKeyboard.h>
#include <esp_task_wdt.h>
#include <tusb.h>
#include <set>
#include <vector>

// ═══════════════════════════════════════════════════════════════════════════
// CONNECTION STATE & TRANSPORT TYPES
// ═══════════════════════════════════════════════════════════════════════════

enum class ConnectionState {
    DISCOVERY,   // Cycling through transports/endpoints looking for host
    LOCKED       // Locked to a specific transport/endpoint
};

enum class TransportType {
    MQTT,
    WEBSOCKET,
    HTTP
};

struct LockInfo {
    TransportType transport;
    uint8_t endpoint_index;
    unsigned long lock_until_ms;  // millis() + TTL
};

// ═══════════════════════════════════════════════════════════════════════════
// GLOBAL STATE
// ═══════════════════════════════════════════════════════════════════════════

static ConnectionState connectionState = ConnectionState::DISCOVERY;
static TransportType currentTransport = TransportType::MQTT;
static LockInfo lockInfo = {TransportType::MQTT, 0, 0};

static USBHIDKeyboard kbd;
static USBHIDMouse Mouse;
static std::set<uint8_t> pressedKeys;

// HID Constants
const int HID_TIMEOUT_MS = 1000;
const int MIN_HID_INTERVAL_MS = 20;
unsigned long lastHidTime = 0;

const char* DEVICE_ID = "esp32_hid_001";

// ═══════════════════════════════════════════════════════════════════════════
// MDNS AUTO-DISCOVERY (New in v5)
// ═══════════════════════════════════════════════════════════════════════════

static WiFiUDP mdnsUdp;
const uint16_t MDNS_PORT = 37020;

struct DiscoveredEndpoint {
    String host;
    uint16_t ws_port;
    uint16_t http_port;
    unsigned long last_seen_ms;
};

std::vector<DiscoveredEndpoint> discoveredEndpoints;
const unsigned long DISCOVERY_TIMEOUT_MS = 60000;  // Forget endpoints after 60s

void startMdnsListener() {
    if (mdnsUdp.begin(MDNS_PORT)) {
        Serial.printf("[mDNS] Listening on UDP port %d\n", MDNS_PORT);
    } else {
        Serial.println("[mDNS] Failed to start UDP listener");
    }
}

void processMdnsAnnouncement() {
    int packetSize = mdnsUdp.parsePacket();
    if (packetSize == 0) {
        return;
    }

    char buffer[512];
    int len = mdnsUdp.read(buffer, sizeof(buffer) - 1);
    if (len <= 0) {
        return;
    }
    buffer[len] = '\0';

    // Parse JSON announcement
    JsonDocument doc;
    DeserializationError error = deserializeJson(doc, buffer);
    if (error) {
        return;
    }

    String service = doc["service"];
    String device_id = doc["device_id"];
    String host = doc["host"];

    // Check if it's for us
    if (service != "hid-tunnel" || device_id != DEVICE_ID) {
        return;
    }

    // Extract port information
    uint16_t ws_port = doc["ports"]["ws"] | 0;
    uint16_t http_port = doc["ports"]["http"] | 0;

    if (host.isEmpty() || (ws_port == 0 && http_port == 0)) {
        return;
    }

    Serial.printf("[mDNS] Discovered: %s (WS:%d, HTTP:%d)\n",
                  host.c_str(), ws_port, http_port);

    // Update or add endpoint
    bool found = false;
    for (auto& ep : discoveredEndpoints) {
        if (ep.host == host) {
            ep.ws_port = ws_port;
            ep.http_port = http_port;
            ep.last_seen_ms = millis();
            found = true;
            break;
        }
    }

    if (!found) {
        DiscoveredEndpoint ep;
        ep.host = host;
        ep.ws_port = ws_port;
        ep.http_port = http_port;
        ep.last_seen_ms = millis();
        discoveredEndpoints.push_back(ep);
        Serial.printf("[mDNS] Added endpoint: %s\n", host.c_str());
    }
}

void cleanupStaleEndpoints() {
    unsigned long now = millis();
    auto it = discoveredEndpoints.begin();
    while (it != discoveredEndpoints.end()) {
        if (now - it->last_seen_ms > DISCOVERY_TIMEOUT_MS) {
            Serial.printf("[mDNS] Removing stale endpoint: %s\n", it->host.c_str());
            it = discoveredEndpoints.erase(it);
        } else {
            ++it;
        }
    }
}

// ═══════════════════════════════════════════════════════════════════════════
// MQTT TRANSPORT
// ═══════════════════════════════════════════════════════════════════════════

static AsyncMqttClient mqttClient;
static TimerHandle_t mqttReconnectTimer;
static TimerHandle_t hidTimeoutTimer;

struct MQTTBrokerConfig {
    const char* host;
    uint16_t port;
};

MQTTBrokerConfig MQTT_BROKERS[] = {
    {"broker.emqx.io", 1883},
    {"test.mosquitto.org", 1883},
};

const size_t MQTT_BROKER_COUNT = sizeof(MQTT_BROKERS) / sizeof(MQTT_BROKERS[0]);
size_t currentMqttBrokerIndex = 0;
uint8_t mqttBrokerFailureCount = 0;
const uint8_t MAX_BROKER_FAILURES = 3;

// Topics
String mouseTopic = "hid/" + String(DEVICE_ID) + "/mouse";
String keyTopic = "hid/" + String(DEVICE_ID) + "/key";
String statusTopic = "hid/" + String(DEVICE_ID) + "/status";
String pingTopic = "hid/" + String(DEVICE_ID) + "/ping";

// ═══════════════════════════════════════════════════════════════════════════
// WEBSOCKET TRANSPORT
// ═══════════════════════════════════════════════════════════════════════════

static WebSocketsClient wsClient;
bool wsConnected = false;

struct WSEndpointConfig {
    const char* host;
    uint16_t port;
    const char* path;
};

// Static fallback endpoints (if mDNS fails)
WSEndpointConfig WS_ENDPOINTS[] = {
    {"192.168.1.100", 8765, "/"},
};

const size_t WS_ENDPOINT_COUNT = sizeof(WS_ENDPOINTS) / sizeof(WS_ENDPOINTS[0]);
size_t currentWsEndpointIndex = 0;

// ═══════════════════════════════════════════════════════════════════════════
// HTTP TRANSPORT (New in v5)
// ═══════════════════════════════════════════════════════════════════════════

static HTTPClient httpClient;
bool httpConnected = false;
unsigned long lastHttpPollTime = 0;
const unsigned long HTTP_POLL_INTERVAL_MS = 2000;  // Poll every 2s
const unsigned long HTTP_POLL_TIMEOUT_MS = 25000;  // 25s server timeout

struct HTTPEndpointConfig {
    const char* host;
    uint16_t port;
};

// Static fallback endpoints (if mDNS fails)
HTTPEndpointConfig HTTP_ENDPOINTS[] = {
    {"192.168.1.100", 8080},
};

const size_t HTTP_ENDPOINT_COUNT = sizeof(HTTP_ENDPOINTS) / sizeof(HTTP_ENDPOINTS[0]);
size_t currentHttpEndpointIndex = 0;

String currentHttpHost = "";
uint16_t currentHttpPort = 0;

// ═══════════════════════════════════════════════════════════════════════════
// FORWARD DECLARATIONS
// ═══════════════════════════════════════════════════════════════════════════

void processHIDCommand(const JsonDocument& doc, const String& type);
void sendStatus(const char* status);
void switchTransport();
void checkLockExpiry();

// ═══════════════════════════════════════════════════════════════════════════
// HID CORE (Transport-agnostic)
// ═══════════════════════════════════════════════════════════════════════════

static void hidTimeoutCallback(TimerHandle_t xTimer) {
    kbd.releaseAll();
    Mouse.release(MOUSE_LEFT | MOUSE_RIGHT | MOUSE_MIDDLE);
    pressedKeys.clear();
    Serial.println("[HID] Timeout: Released all");
}

void handleMouseCommand(const JsonDocument& doc) {
    int dx = doc["dx"] | 0;
    int dy = doc["dy"] | 0;
    int wheel = doc["wheel"] | 0;
    String buttonStr = doc["button"] | "";
    String buttonAction = doc["button_action"] | "";

    // Clamp to HID range
    dx = max(-127, min(127, dx));
    dy = max(-127, min(127, dy));
    wheel = max(-127, min(127, wheel));

    // Handle buttons (unthrottled)
    uint8_t button = 0;
    if (buttonStr == "left") button = MOUSE_LEFT;
    else if (buttonStr == "right") button = MOUSE_RIGHT;
    else if (buttonStr == "middle") button = MOUSE_MIDDLE;

    if (button != 0) {
        if (buttonAction == "press") {
            Mouse.press(button);
        } else if (buttonAction == "release") {
            Mouse.release(button);
        } else if (buttonAction == "release_all") {
            Mouse.release(MOUSE_LEFT | MOUSE_RIGHT | MOUSE_MIDDLE);
        }
        Mouse.move(0, 0, 0);  // Nudge
    }

    // Throttle movement
    if (dx != 0 || dy != 0 || wheel != 0) {
        unsigned long now = millis();
        if (now - lastHidTime >= MIN_HID_INTERVAL_MS) {
            Mouse.move(dx, dy, wheel);
            lastHidTime = now;
        }
    }
}

void handleKeyCommand(const JsonDocument& doc) {
    String action = doc["action"];
    int keyCode = doc["key"] | 0;

    unsigned long now = millis();
    if (now - lastHidTime < MIN_HID_INTERVAL_MS) {
        return;  // Throttle
    }

    // State-based protocol
    if (action == "state") {
        JsonArray pressedArray = doc["pressed"];
        std::set<uint8_t> newPressed;

        for (JsonVariant v : pressedArray) {
            uint8_t key = v.as<uint8_t>();
            newPressed.insert(key);
        }

        // Release keys no longer pressed
        for (uint8_t key : pressedKeys) {
            if (newPressed.find(key) == newPressed.end()) {
                kbd.release(key);
            }
        }

        // Press newly pressed keys
        for (uint8_t key : newPressed) {
            if (pressedKeys.find(key) == pressedKeys.end()) {
                kbd.press(key);
            }
        }

        pressedKeys = newPressed;
    }
    // Legacy event-based protocol
    else if (action == "press") {
        kbd.press(keyCode);
        pressedKeys.insert(keyCode);
    } else if (action == "release") {
        kbd.release(keyCode);
        pressedKeys.erase(keyCode);
    } else if (action == "release_all") {
        kbd.releaseAll();
        pressedKeys.clear();
    }

    lastHidTime = now;
}

void handleControlCommand(const JsonDocument& doc) {
    String command = doc["command"];

    if (command == "lock_transport") {
        // Host wants to lock to a specific transport
        String transport_str = doc["transport"];
        uint8_t endpoint_idx = doc["endpoint_index"] | 0;
        unsigned long ttl_s = doc["lock_ttl_s"] | 86400;  // Default 24h

        TransportType transport = TransportType::MQTT;
        if (transport_str == "mqtt") transport = TransportType::MQTT;
        else if (transport_str == "ws") transport = TransportType::WEBSOCKET;
        else if (transport_str == "http") transport = TransportType::HTTP;

        // Only lock if it matches our current transport
        if (transport == currentTransport) {
            lockInfo.transport = transport;
            lockInfo.endpoint_index = endpoint_idx;
            lockInfo.lock_until_ms = millis() + (ttl_s * 1000);
            connectionState = ConnectionState::LOCKED;
            Serial.printf("[CONTROL] Locked to %s endpoint %d for %lu s\n",
                          transport_str.c_str(), endpoint_idx, ttl_s);
            sendStatus("locked");
        }
    }
    else if (command == "unlock_transport") {
        connectionState = ConnectionState::DISCOVERY;
        Serial.println("[CONTROL] Unlocked, entering discovery");
        sendStatus("discovery");
    }
}

void processHIDCommand(const JsonDocument& doc, const String& msgType) {
    if (msgType == "mouse") {
        handleMouseCommand(doc);
    } else if (msgType == "key") {
        handleKeyCommand(doc);
    } else if (msgType == "control") {
        handleControlCommand(doc);
    } else if (msgType == "ping") {
        sendStatus("alive");
    }

    // Reset safety timers
    xTimerReset(hidTimeoutTimer, 0);
    esp_task_wdt_reset();
}

// ═══════════════════════════════════════════════════════════════════════════
// MQTT TRANSPORT IMPLEMENTATION
// ═══════════════════════════════════════════════════════════════════════════

void connectToMqtt() {
    MQTTBrokerConfig& broker = MQTT_BROKERS[currentMqttBrokerIndex];
    Serial.printf("[MQTT] Connecting to [%d/%d]: %s:%d\n",
                  currentMqttBrokerIndex + 1, MQTT_BROKER_COUNT,
                  broker.host, broker.port);

    mqttClient.setServer(broker.host, broker.port);
    mqttClient.connect();
}

void onMqttConnect(bool sessionPresent) {
    Serial.println("[MQTT] ✓ Connected");
    mqttBrokerFailureCount = 0;

    // Subscribe
    mqttClient.subscribe(mouseTopic.c_str(), 0);
    mqttClient.subscribe(keyTopic.c_str(), 1);
    mqttClient.subscribe(pingTopic.c_str(), 1);

    sendStatus("online");
}

void onMqttDisconnect(AsyncMqttClientDisconnectReason reason) {
    Serial.printf("[MQTT] ✗ Disconnected (reason: %d)\n", static_cast<int>(reason));

    // Release HID controls
    kbd.releaseAll();
    Mouse.release(MOUSE_LEFT | MOUSE_RIGHT | MOUSE_MIDDLE);
    pressedKeys.clear();

    if (WiFi.isConnected()) {
        mqttBrokerFailureCount++;

        // If locked and current broker failed, consider unlocking
        if (connectionState == ConnectionState::LOCKED &&
            lockInfo.transport == TransportType::MQTT &&
            lockInfo.endpoint_index == currentMqttBrokerIndex) {
            // Check if lock expired
            if (millis() > lockInfo.lock_until_ms) {
                Serial.println("[MQTT] Lock expired, entering discovery");
                connectionState = ConnectionState::DISCOVERY;
            }
        }

        // Rotate broker if not locked
        if (connectionState == ConnectionState::DISCOVERY &&
            mqttBrokerFailureCount >= MAX_BROKER_FAILURES) {
            currentMqttBrokerIndex = (currentMqttBrokerIndex + 1) % MQTT_BROKER_COUNT;
            mqttBrokerFailureCount = 0;
            Serial.printf("[MQTT] Rotating to broker [%d]\n", currentMqttBrokerIndex);
        }

        xTimerStart(mqttReconnectTimer, 0);
    }
}

void onMqttMessage(char* topic, char* payload, AsyncMqttClientMessageProperties properties,
                   size_t len, size_t index, size_t total) {
    if (len >= 512) {
        Serial.println("[MQTT] Payload too large");
        return;
    }
    payload[len] = '\0';

    JsonDocument doc;
    DeserializationError error = deserializeJson(doc, payload);
    if (error) {
        Serial.printf("[MQTT] JSON error: %s\n", error.c_str());
        return;
    }

    String topicStr = String(topic);
    String msgType = "";

    if (topicStr == mouseTopic) msgType = "mouse";
    else if (topicStr == keyTopic) msgType = "key";
    else if (topicStr == pingTopic) {
        String from = doc["from"];
        if (from == "host") msgType = "ping";
    }

    if (!msgType.isEmpty()) {
        processHIDCommand(doc, msgType);
    }
}

// ═══════════════════════════════════════════════════════════════════════════
// WEBSOCKET TRANSPORT IMPLEMENTATION
// ═══════════════════════════════════════════════════════════════════════════

void connectToWebSocket() {
    // Try discovered endpoints first
    if (!discoveredEndpoints.empty()) {
        for (auto& ep : discoveredEndpoints) {
            if (ep.ws_port > 0) {
                Serial.printf("[WS] Connecting to discovered: ws://%s:%d/\n",
                              ep.host.c_str(), ep.ws_port);
                wsClient.begin(ep.host.c_str(), ep.ws_port, "/");
                return;
            }
        }
    }

    // Fallback to static config
    WSEndpointConfig& endpoint = WS_ENDPOINTS[currentWsEndpointIndex];
    Serial.printf("[WS] Connecting to static [%d/%d]: ws://%s:%d%s\n",
                  currentWsEndpointIndex + 1, WS_ENDPOINT_COUNT,
                  endpoint.host, endpoint.port, endpoint.path);

    wsClient.begin(endpoint.host, endpoint.port, endpoint.path);
}

void onWebSocketEvent(WStype_t type, uint8_t* payload, size_t length) {
    switch (type) {
        case WStype_DISCONNECTED:
            Serial.println("[WS] ✗ Disconnected");
            wsConnected = false;

            // Release HID controls
            kbd.releaseAll();
            Mouse.release(MOUSE_LEFT | MOUSE_RIGHT | MOUSE_MIDDLE);
            pressedKeys.clear();

            // If locked, check expiry
            if (connectionState == ConnectionState::LOCKED &&
                lockInfo.transport == TransportType::WEBSOCKET) {
                if (millis() > lockInfo.lock_until_ms) {
                    connectionState = ConnectionState::DISCOVERY;
                }
            }
            break;

        case WStype_CONNECTED:
            Serial.printf("[WS] ✓ Connected to %s\n", payload);
            wsConnected = true;
            sendStatus("online");
            break;

        case WStype_TEXT: {
            // Parse JSON message
            JsonDocument doc;
            DeserializationError error = deserializeJson(doc, payload, length);
            if (error) {
                Serial.printf("[WS] JSON error: %s\n", error.c_str());
                return;
            }

            String msgType = doc["type"];
            processHIDCommand(doc, msgType);
            break;
        }

        default:
            break;
    }
}

// ═══════════════════════════════════════════════════════════════════════════
// HTTP TRANSPORT IMPLEMENTATION (New in v5)
// ═══════════════════════════════════════════════════════════════════════════

void connectToHttp() {
    // Try discovered endpoints first
    if (!discoveredEndpoints.empty()) {
        for (auto& ep : discoveredEndpoints) {
            if (ep.http_port > 0) {
                currentHttpHost = ep.host;
                currentHttpPort = ep.http_port;
                httpConnected = true;
                Serial.printf("[HTTP] Using discovered: http://%s:%d\n",
                              currentHttpHost.c_str(), currentHttpPort);
                return;
            }
        }
    }

    // Fallback to static config
    HTTPEndpointConfig& endpoint = HTTP_ENDPOINTS[currentHttpEndpointIndex];
    currentHttpHost = String(endpoint.host);
    currentHttpPort = endpoint.port;
    httpConnected = true;
    Serial.printf("[HTTP] Using static [%d/%d]: http://%s:%d\n",
                  currentHttpEndpointIndex + 1, HTTP_ENDPOINT_COUNT,
                  endpoint.host, endpoint.port);
}

void httpPoll() {
    if (!httpConnected) {
        return;
    }

    unsigned long now = millis();
    if (now - lastHttpPollTime < HTTP_POLL_INTERVAL_MS) {
        return;  // Don't poll too frequently
    }

    String url = "http://" + currentHttpHost + ":" + String(currentHttpPort) +
                 "/poll?device_id=" + String(DEVICE_ID);

    httpClient.setTimeout(HTTP_POLL_TIMEOUT_MS);
    httpClient.begin(url);

    int httpCode = httpClient.GET();
    lastHttpPollTime = now;

    if (httpCode == 200) {
        String payload = httpClient.getString();

        // Parse command
        JsonDocument doc;
        DeserializationError error = deserializeJson(doc, payload);
        if (!error) {
            String msgType = doc["type"];

            // Ignore heartbeats
            if (msgType != "heartbeat") {
                processHIDCommand(doc, msgType);
            }
        }
    } else if (httpCode < 0) {
        Serial.printf("[HTTP] Poll failed: %s\n", httpClient.errorToString(httpCode).c_str());
        httpConnected = false;

        // If locked, check expiry
        if (connectionState == ConnectionState::LOCKED &&
            lockInfo.transport == TransportType::HTTP) {
            if (millis() > lockInfo.lock_until_ms) {
                connectionState = ConnectionState::DISCOVERY;
            }
        }
    }

    httpClient.end();
}

void httpSendStatus(const JsonDocument& statusDoc) {
    if (!httpConnected) {
        return;
    }

    String url = "http://" + currentHttpHost + ":" + String(currentHttpPort) + "/status";

    String payload;
    serializeJson(statusDoc, payload);

    httpClient.begin(url);
    httpClient.addHeader("Content-Type", "application/json");

    int httpCode = httpClient.POST(payload);

    if (httpCode < 0) {
        Serial.printf("[HTTP] Status send failed: %s\n", httpClient.errorToString(httpCode).c_str());
    }

    httpClient.end();
}

// ═══════════════════════════════════════════════════════════════════════════
// STATUS REPORTING (All transports)
// ═══════════════════════════════════════════════════════════════════════════

void sendStatus(const char* status) {
    JsonDocument doc;
    doc["status"] = status;
    doc["device_id"] = DEVICE_ID;

    String transport_str = "mqtt";
    if (currentTransport == TransportType::WEBSOCKET) transport_str = "ws";
    else if (currentTransport == TransportType::HTTP) transport_str = "http";

    doc["transport"] = transport_str;
    doc["endpoint_index"] = 0;  // Simplified for auto-discovery
    doc["connection_state"] = (connectionState == ConnectionState::DISCOVERY) ? "discovery" : "locked";
    doc["usb_connected"] = tud_mounted();
    doc["pressed_keys_count"] = pressedKeys.size();
    doc["uptime_ms"] = millis();
    doc["free_heap"] = ESP.getFreeHeap();
    doc["keyboard_state_supported"] = true;
    doc["discovered_endpoints"] = discoveredEndpoints.size();

    String payload;
    serializeJson(doc, payload);

    // Send via active transport
    if (currentTransport == TransportType::MQTT && mqttClient.connected()) {
        mqttClient.publish(statusTopic.c_str(), 1, true, payload.c_str());
    } else if (currentTransport == TransportType::WEBSOCKET && wsConnected) {
        JsonDocument wsDoc;
        wsDoc["type"] = "status";
        wsDoc.set(doc);
        String wsPayload;
        serializeJson(wsDoc, wsPayload);
        wsClient.sendTXT(wsPayload);
    } else if (currentTransport == TransportType::HTTP && httpConnected) {
        httpSendStatus(doc);
    }
}

// ═══════════════════════════════════════════════════════════════════════════
// TRANSPORT SWITCHING & DISCOVERY
// ═══════════════════════════════════════════════════════════════════════════

void switchTransport() {
    // Only switch if in DISCOVERY mode
    if (connectionState != ConnectionState::DISCOVERY) {
        return;
    }

    Serial.println("[TRANSPORT] Switching...");

    // Disconnect current transport
    if (currentTransport == TransportType::MQTT) {
        mqttClient.disconnect();
        // Try WebSocket next
        currentTransport = TransportType::WEBSOCKET;
        connectToWebSocket();
    } else if (currentTransport == TransportType::WEBSOCKET) {
        wsClient.disconnect();
        wsConnected = false;
        // Try HTTP next
        currentTransport = TransportType::HTTP;
        connectToHttp();
    } else if (currentTransport == TransportType::HTTP) {
        httpConnected = false;
        // Back to MQTT
        currentTransport = TransportType::MQTT;
        connectToMqtt();
    }
}

void checkLockExpiry() {
    if (connectionState == ConnectionState::LOCKED) {
        if (millis() > lockInfo.lock_until_ms) {
            Serial.println("[LOCK] Expired, entering discovery");
            connectionState = ConnectionState::DISCOVERY;
            switchTransport();
        }
    }
}

// ═══════════════════════════════════════════════════════════════════════════
// INITIALIZATION
// ═══════════════════════════════════════════════════════════════════════════

void duck_control_web_begin() {
    Serial.println("[DUCK] Initializing v5.0 - Phase 2...");

    // Initialize HID
    Mouse.begin();
    kbd.begin();
    Serial.println("[HID] Initialized");

    // Setup timers
    mqttReconnectTimer = xTimerCreate(
        "mqttTimer",
        pdMS_TO_TICKS(2000),
        pdFALSE,
        (void*)0,
        reinterpret_cast<TimerCallbackFunction_t>(connectToMqtt)
    );

    hidTimeoutTimer = xTimerCreate(
        "hidTimeout",
        pdMS_TO_TICKS(HID_TIMEOUT_MS),
        pdFALSE,
        (void*)0,
        hidTimeoutCallback
    );

    // Init watchdog
    esp_task_wdt_init(5, false);
    esp_task_wdt_add(NULL);
    Serial.println("[WDT] Initialized");

    // Setup MQTT
    mqttClient.onConnect(onMqttConnect);
    mqttClient.onDisconnect(onMqttDisconnect);
    mqttClient.onMessage(onMqttMessage);

    // Setup WebSocket
    wsClient.onEvent(onWebSocketEvent);

    // Start mDNS discovery listener
    startMdnsListener();

    // Start with MQTT in discovery mode
    Serial.println("[TRANSPORT] Starting discovery with MQTT...");
    connectToMqtt();

    Serial.println("[DUCK] ✓ Initialized");
    Serial.printf("MQTT brokers: %d\n", MQTT_BROKER_COUNT);
    Serial.printf("WS static endpoints: %d\n", WS_ENDPOINT_COUNT);
    Serial.printf("HTTP static endpoints: %d\n", HTTP_ENDPOINT_COUNT);
    Serial.println("Listening for mDNS broadcasts on port 37020");
}

void duck_control_mqtt_loop() {
    // Periodic tasks
    static unsigned long lastCheck = 0;
    unsigned long now = millis();

    // Process mDNS announcements
    processMdnsAnnouncement();

    // Clean up stale endpoints every 30 seconds
    static unsigned long lastCleanup = 0;
    if (now - lastCleanup > 30000) {
        cleanupStaleEndpoints();
        lastCleanup = now;
    }

    // Health check every 5 seconds
    if (now - lastCheck > 5000) {
        const char* transport_name = "MQTT";
        if (currentTransport == TransportType::WEBSOCKET) transport_name = "WS";
        else if (currentTransport == TransportType::HTTP) transport_name = "HTTP";

        Serial.printf("[HEALTH] State: %s, Transport: %s, USB: %s, Keys: %d, Heap: %u, Discovered: %d\n",
                      (connectionState == ConnectionState::DISCOVERY) ? "DISCOVERY" : "LOCKED",
                      transport_name,
                      tud_mounted() ? "OK" : "DISC",
                      pressedKeys.size(),
                      ESP.getFreeHeap(),
                      discoveredEndpoints.size());
        lastCheck = now;
    }

    // Transport-specific loops
    if (currentTransport == TransportType::WEBSOCKET) {
        wsClient.loop();
    } else if (currentTransport == TransportType::HTTP) {
        httpPoll();
    }

    // Check lock expiry
    checkLockExpiry();

    // Transport switching logic
    static unsigned long lastSwitchAttempt = 0;
    if (connectionState == ConnectionState::DISCOVERY &&
        now - lastSwitchAttempt > 30000) {  // Try switching every 30s if no connection
        bool connected = false;

        if (currentTransport == TransportType::MQTT) {
            connected = mqttClient.connected();
        } else if (currentTransport == TransportType::WEBSOCKET) {
            connected = wsConnected;
        } else if (currentTransport == TransportType::HTTP) {
            connected = httpConnected;
        }

        if (!connected) {
            switchTransport();
            lastSwitchAttempt = now;
        }
    }
}
