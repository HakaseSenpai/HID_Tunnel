/*
 * MQTT-based HID control for ESP32
 * Receives mouse/keyboard commands via MQTT and executes them
 */

#include "duck_control_web.h"
#include <Arduino.h>
#include <WiFi.h>
#include <ArduinoJson.h>
#include <AsyncMqttClient.h>
#include <USB.h>
#include <USBHIDMouse.h>
#include <USBHIDKeyboard.h>
#include <esp_task_wdt.h>  // For watchdog timer (lightweight, battle-tested)
#include <tusb.h>  // For TinyUSB state checks on ESP32-S2

static AsyncMqttClient mqttClient;
static TimerHandle_t mqttReconnectTimer;
static TimerHandle_t hidTimeoutTimer;  // Timer for HID release on inactivity
static USBHIDKeyboard kbd;
static USBHIDMouse Mouse;

// MQTT Configuration
const char* MQTT_HOST = "broker.emqx.io";
const int MQTT_PORT = 1883;
const char* DEVICE_ID = "esp32_hid_001";  // Should match Python script

// Topics
String mouseTopic = "hid/" + String(DEVICE_ID) + "/mouse";
String keyTopic = "hid/" + String(DEVICE_ID) + "/key";
String statusTopic = "hid/" + String(DEVICE_ID) + "/status";
String pingTopic = "hid/" + String(DEVICE_ID) + "/ping";

// HID Constants
const int HID_TIMEOUT_MS = 1000;  // Inactivity timeout for auto-release
const int MIN_HID_INTERVAL_MS = 50;  // Min time between HID commands to smooth latency
unsigned long lastHidTime = 0;  // Track last HID action time

// Separate callback for HID timeout (fixes lambda cast error)
static void hidTimeoutCallback(TimerHandle_t xTimer) {
    kbd.releaseAll();  // Release all keys
    Mouse.release(MOUSE_LEFT | MOUSE_RIGHT | MOUSE_MIDDLE);  // Reset mouse buttons
    Serial.println("HID timeout: Released all keys and mouse buttons");
}

void connectToMqtt() {
    Serial.println("Connecting to MQTT...");
    mqttClient.connect();
}

void onMqttConnect(bool sessionPresent) {
    Serial.println("Connected to MQTT.");
    Serial.print("Session present: ");
    Serial.println(sessionPresent);

    // Subscribe to command topics
    mqttClient.subscribe(mouseTopic.c_str(), 2);
    mqttClient.subscribe(keyTopic.c_str(), 2);
    mqttClient.subscribe(pingTopic.c_str(), 2);  // New: Subscribe to ping for alive/status

    Serial.print("Subscribing to mouse topic: ");
    Serial.println(mouseTopic);
    Serial.print("Subscribing to key topic: ");
    Serial.println(keyTopic);
    Serial.print("Subscribing to ping topic: ");
    Serial.println(pingTopic);

    // Publish online status (fixed for JsonDocument)
    JsonDocument statusDoc;
    statusDoc["status"] = "online";
    statusDoc["device"] = DEVICE_ID;
    statusDoc["timestamp"] = millis();

    String statusPayload;
    serializeJson(statusDoc, statusPayload);
    mqttClient.publish(statusTopic.c_str(), 0, true, statusPayload.c_str());
}

void onMqttDisconnect(AsyncMqttClientDisconnectReason reason) {
    Serial.println("Disconnected from MQTT.");

    if (WiFi.isConnected()) {
        xTimerStart(mqttReconnectTimer, 0);
    }
}

void onMqttMessage(char* topic, char* payload, AsyncMqttClientMessageProperties properties, size_t len, size_t index, size_t total) {
    // // Null terminate payload
    payload[len] = '\0';

    Serial.print("Message arrived [");
    Serial.print(topic);
    Serial.print("]: ");
    Serial.println(payload);

    // Timing measurement start
    unsigned long startTime = millis();

    // Parse JSON payload (fixed for JsonDocument)
    JsonDocument doc;
    DeserializationError error = deserializeJson(doc, payload);

    if (error) {
        Serial.print("JSON parsing failed: ");
        Serial.println(error.c_str());
        return;
    }

    String topicStr = String(topic);
    //
    // if (topicStr == mouseTopic) {
    //     // Handle mouse command
    //     int dx = doc["dx"] | 0;
    //     int dy = doc["dy"] | 0;
    //     int wheel = doc["wheel"] | 0;
    //
    //     // Clamp to valid range (-127 to 127)
    //     dx = max(-127, min(127, dx));
    //     dy = max(-127, min(127, dy));
    //     wheel = max(-127, min(127, wheel));
    //
    //     // Throttle to min interval
    //     if (millis() - lastHidTime >= MIN_HID_INTERVAL_MS) {
    //         Mouse.move(dx, dy, wheel);
    //         lastHidTime = millis();
    //         Serial.printf("Mouse moved: dx=%d, dy=%d, wheel=%d\n", dx, dy, wheel);
    //     } else {
    //         Serial.println("Mouse command throttled due to min interval");
    //     }
    if (topicStr == mouseTopic) {
        // Handle mouse command (now including buttons)
        int dx = doc["dx"] | 0;
        int dy = doc["dy"] | 0;
        int wheel = doc["wheel"] | 0;
        String buttonStr = doc["button"] | "";
        String buttonAction = doc["button_action"] | "";

        // Clamp movement to valid range (-127 to 127)
        dx = max(-127, min(127, dx));
        dy = max(-127, min(127, dy));
        wheel = max(-127, min(127, wheel));

        // Map button string to HID constant
        uint8_t button = 0;
        if (buttonStr == "left") button = MOUSE_LEFT;
        else if (buttonStr == "right") button = MOUSE_RIGHT;
        else if (buttonStr == "middle") button = MOUSE_MIDDLE;
        else if (!buttonStr.isEmpty()) {
            Serial.printf("Invalid button '%s' ignored\n", buttonStr.c_str());
        }

        // Always handle buttons (don't throttle clicks)
        if (button != 0) {
            if (buttonAction == "press") {
                Mouse.press(button);
                Serial.printf("Mouse button pressed: %s\n", buttonStr.c_str());
            } else if (buttonAction == "release") {
                Mouse.release(button);
                Serial.printf("Mouse button released: %s\n", buttonStr.c_str());
            } else if (buttonAction == "release_all") {
                Mouse.release(MOUSE_LEFT | MOUSE_RIGHT | MOUSE_MIDDLE);
                Serial.println("All mouse buttons released");
            } else {
                Serial.printf("Invalid button_action '%s' ignored\n", buttonAction.c_str());
            }
            // Nudge with zero move to force HID report (helps with TinyUSB quirks)
            Mouse.move(0, 0, 0);
        }

        // Throttle only movement
        if (dx != 0 || dy != 0 || wheel != 0) {  // Only throttle if there's actual movement
            if (millis() - lastHidTime >= MIN_HID_INTERVAL_MS) {
                Mouse.move(dx, dy, wheel);
                Serial.printf("Mouse moved: dx=%d, dy=%d, wheel=%d\n", dx, dy, wheel);
                lastHidTime = millis();
            } else {
                Serial.println("Mouse movement throttled due to min interval");
            }
        } else if (button == 0) {
            Serial.println("Received mouse message with no action (ignored)");
        }


    } else if (topicStr == keyTopic) {
        // Handle keyboard command
        String action = doc["action"];
        int keyCode = doc["key"] | 0;

        // Validate keyCode (0-255)
        if (keyCode < 0 || keyCode > 255) {
            Serial.printf("Invalid keyCode %d ignored\n", keyCode);
            return;
        }

        // Throttle to min interval
        if (millis() - lastHidTime >= MIN_HID_INTERVAL_MS) {
            if (action == "press") {
                kbd.press(keyCode);
                Serial.printf("Key pressed: %d\n", keyCode);
            } else if (action == "release") {
                kbd.release(keyCode);
                Serial.printf("Key released: %d\n", keyCode);
            } else if (action == "release_all") {
                kbd.releaseAll();
                Serial.println("All keys released");
            }
            lastHidTime = millis();
        } else {
            Serial.println("Keyboard command throttled due to min interval");
        }

    } else if (topicStr == pingTopic) {
        // Handle ping for alive/status (fixed for JsonDocument)
        JsonDocument statusDoc;
        statusDoc["status"] = "alive";
        statusDoc["usb_connected"] = tud_mounted();  // Fixed: Use TinyUSB check for USB HID connected
        statusDoc["timestamp"] = millis();
        String payloadStr;  // Renamed to avoid conflict
        serializeJson(statusDoc, payloadStr);
        mqttClient.publish(statusTopic.c_str(), 0, true, payloadStr.c_str());
        Serial.println("Sent alive status");
    }

    // Timing measurement end
    unsigned long endTime = millis();
    Serial.printf("Message processed in %lu ms\n", endTime - startTime);

    // Reset HID timeout timer and watchdog on activity
    xTimerReset(hidTimeoutTimer, 0);
    esp_task_wdt_reset();
}

void duck_control_web_begin() {
    // Initialize HID devices
    Mouse.begin();
    kbd.begin();

    // Setup MQTT reconnect timer
    mqttReconnectTimer = xTimerCreate("mqttTimer", pdMS_TO_TICKS(2000), pdFALSE, (void*)0, reinterpret_cast<TimerCallbackFunction_t>(connectToMqtt));

    // Setup HID timeout timer (pass separate callback function)
    hidTimeoutTimer = xTimerCreate("hidTimeout", pdMS_TO_TICKS(HID_TIMEOUT_MS), pdFALSE, (void*)0, hidTimeoutCallback);

    // Init watchdog (5s timeout, no panic)
    esp_task_wdt_init(5, false);
    esp_task_wdt_add(NULL);  // Add current task to watchdog

    // Setup MQTT client
    mqttClient.onConnect(onMqttConnect);
    mqttClient.onDisconnect(onMqttDisconnect);
    mqttClient.onMessage(onMqttMessage);
    mqttClient.setServer(MQTT_HOST, MQTT_PORT);

    // Connect to MQTT
    connectToMqtt();

    Serial.println("MQTT HID control initialized");
}

void duck_control_mqtt_loop() {
    // Keep MQTT connection alive (AsyncMqttClient handles this)
    // Periodic check for overload (every 1s)
    static unsigned long lastCheck = 0;
    if (millis() - lastCheck > 1000) {
        Serial.printf("MQTT loop: Free heap %u bytes\n", ESP.getFreeHeap());  // Check memory
        lastCheck = millis();
    }
}





// /*
//  * MQTT-based HID control for ESP32
//  * Receives mouse/keyboard commands via MQTT and executes them
//  */
//
// #include "duck_control_web.h"
// #include <Arduino.h>
// #include <WiFi.h>
// #include <ArduinoJson.h>
// #include <AsyncMqttClient.h>
// #include <USB.h>
// #include <USBHIDMouse.h>
// #include <USBHIDKeyboard.h>
//
// static AsyncMqttClient mqttClient;
// static TimerHandle_t mqttReconnectTimer;
// static USBHIDKeyboard kbd;
// static USBHIDMouse Mouse;
//
// // MQTT Configuration
// const char* MQTT_HOST = "broker.emqx.io";
// const int MQTT_PORT = 1883;
// const char* DEVICE_ID = "esp32_hid_001";  // Should match Python script
//
// // Topics
// String mouseTopic = "hid/" + String(DEVICE_ID) + "/mouse";
// String keyTopic = "hid/" + String(DEVICE_ID) + "/key";
// String statusTopic = "hid/" + String(DEVICE_ID) + "/status";
//
// void connectToMqtt() {
//     Serial.println("Connecting to MQTT...");
//     mqttClient.connect();
// }
//
// void onMqttConnect(bool sessionPresent) {
//     Serial.println("Connected to MQTT.");
//     Serial.print("Session present: ");
//     Serial.println(sessionPresent);
//
//     // Subscribe to command topics
//     uint16_t packetIdSub1 = mqttClient.subscribe(mouseTopic.c_str(), 2);
//     uint16_t packetIdSub2 = mqttClient.subscribe(keyTopic.c_str(), 2);
//
//     Serial.print("Subscribing to mouse topic: ");
//     Serial.println(mouseTopic);
//     Serial.print("Subscribing to key topic: ");
//     Serial.println(keyTopic);
//
//     // Publish online status
//     StaticJsonDocument<200> statusDoc;
//     statusDoc["status"] = "online";
//     statusDoc["device"] = DEVICE_ID;
//     statusDoc["timestamp"] = millis();
//
//     String statusPayload;
//     serializeJson(statusDoc, statusPayload);
//     mqttClient.publish(statusTopic.c_str(), 0, true, statusPayload.c_str());
// }
//
// void onMqttDisconnect(AsyncMqttClientDisconnectReason reason) {
//     Serial.println("Disconnected from MQTT.");
//
//     if (WiFi.isConnected()) {
//         xTimerStart(mqttReconnectTimer, 0);
//     }
// }
//
// void onMqttMessage(char* topic, char* payload, AsyncMqttClientMessageProperties properties, size_t len, size_t index, size_t total) {
//     // Null terminate payload
//     payload[len] = '\0';
//
//     Serial.print("Message arrived [");
//     Serial.print(topic);
//     Serial.print("]: ");
//     Serial.println(payload);
//
//     // Parse JSON payload
//     StaticJsonDocument<200> doc;
//     DeserializationError error = deserializeJson(doc, payload);
//
//     if (error) {
//         Serial.print("JSON parsing failed: ");
//         Serial.println(error.c_str());
//         return;
//     }
//
//     String topicStr = String(topic);
//
//     if (topicStr == mouseTopic) {
//         // Handle mouse command
//         int dx = doc["dx"] | 0;
//         int dy = doc["dy"] | 0;
//         int wheel = doc["wheel"] | 0;
//
//         Mouse.move(dx, dy, wheel);
//         Serial.printf("Mouse moved: dx=%d, dy=%d, wheel=%d\n", dx, dy, wheel);
//
//     } else if (topicStr == keyTopic) {
//         // Handle keyboard command
//         String action = doc["action"];
//         int keyCode = doc["key"] | 0;
//
//         if (action == "press") {
//             kbd.press(keyCode);
//             Serial.printf("Key pressed: %d\n", keyCode);
//         } else if (action == "release") {
//             kbd.release(keyCode);
//             Serial.printf("Key released: %d\n", keyCode);
//         } else if (action == "release_all") {
//             kbd.releaseAll();
//             Serial.println("All keys released");
//         }
//     }
// }
//
// void duck_control_web_begin() {
//     // Initialize HID devices
//     Mouse.begin();
//     kbd.begin();
//
//     // Setup MQTT reconnect timer
//     mqttReconnectTimer = xTimerCreate("mqttTimer", pdMS_TO_TICKS(2000), pdFALSE, (void*)0, reinterpret_cast<TimerCallbackFunction_t>(connectToMqtt));
//
//     // Setup MQTT client
//     mqttClient.onConnect(onMqttConnect);
//     mqttClient.onDisconnect(onMqttDisconnect);
//     mqttClient.onMessage(onMqttMessage);
//     mqttClient.setServer(MQTT_HOST, MQTT_PORT);
//
//     // Connect to MQTT
//     connectToMqtt();
//
//     Serial.println("MQTT HID control initialized");
// }
//
// void duck_control_mqtt_loop() {
//     // Keep MQTT connection alive
//     // The AsyncMqttClient handles this automatically
// }
//
//
//
//
//
//
// // /*
// //  *   Minimal “second server” running on port 81
// //  *   – /status         → {"ready":true}
// //  *   – /mouse?dx=x&dy=y[&wheel=w]
// //  *   – /key?press=kc   (press 1 HID code)
// //  *   – /key?release=kc (release 1 HID code)
// //  *   – /key            (no params → releaseAll)
// //  *
// //  *   Dependencies already present in your build:
// //  *     <WiFi.h>              (comes with ESP32 Arduino core)
// //  *     <ESPAsyncWebServer.h> (you installed)
// //  *     <ArduinoJson.h>       (you installed)
// //  *     <USB.h>, <Mouse.h>,
// //  *     <USBHIDKeyboard.h>    (TinyUSB wrapper in ESP32-S2 core)
// //  */
// // #include "duck_control_web.h"
// //
// // #include <Arduino.h>
// // #include <WiFi.h>
// // #include <ArduinoJson.h>
// // #include <ESPAsyncWebServer.h>
// //
// // #include <USB.h>
// // #include <USBHIDMouse.h>      // <-- new
// //
// // #include <USBHIDKeyboard.h>     // class USBHIDKeyboard
// //
// // static AsyncWebServer duckServer(81);     // keeps clear of the main UI on :80
// // static USBHIDKeyboard kbd;
// // static USBHIDMouse Mouse;
// //
// // // namespace duck_api {
// // //
// // //     void attach(AsyncWebServer &srv)          //  ← exactly this signature
// // //     {
// // //         srv.on("/api/mouse",   HTTP_GET, handleMouse);
// // //         srv.on("/api/key",     HTTP_GET, handleKey);
// // //         srv.on("/api/status",  HTTP_GET, handleStatus);
// // //     }
// // //
// // // } // namespace duck_api
// //
// // // ---------- helpers --------------------------------------------------------
// // static void replyOK(AsyncWebServerRequest *req, const JsonDocument &doc)
// // {
// //     String payload;
// //     serializeJson(doc, payload);
// //     req->send(200, "application/json", payload);
// // }
// //
// // // ---------- /mouse ---------------------------------------------------------
// // static void handleMouse(AsyncWebServerRequest *req)
// // {
// //     int dx = req->hasParam("dx") ? req->getParam("dx")->value().toInt() : 0;
// //     int dy = req->hasParam("dy") ? req->getParam("dy")->value().toInt() : 0;
// //     int wh = req->hasParam("wheel") ? req->getParam("wheel")->value().toInt() : 0;
// //
// //     Mouse.move(dx, dy, wh);
// //
// //     StaticJsonDocument<64> doc;
// //     doc["moved"] = true;
// //     replyOK(req, doc);
// // }
// //
// // // ---------- /key -----------------------------------------------------------
// // static void handleKey(AsyncWebServerRequest *req)
// // {
// //     uint8_t press   = req->hasParam("press")   ? req->getParam("press")->value().toInt()   : 0;
// //     uint8_t release = req->hasParam("release") ? req->getParam("release")->value().toInt() : 0;
// //
// //     if (press)   kbd.press(press);
// //     if (release) kbd.release(release);
// //     if (!press && !release) kbd.releaseAll();      // empty call → release everything
// //
// //     StaticJsonDocument<64> doc;
// //     doc["ok"] = true;
// //     replyOK(req, doc);
// // }
// //
// // // ---------- /status --------------------------------------------------------
// // static void handleStatus(AsyncWebServerRequest *req)
// // {
// //     StaticJsonDocument<64> doc;
// //     doc["ready"] = true;
// //     replyOK(req, doc);
// // }
// //
// // // ---------- public init ----------------------------------------------------
// // void duck_control_web_begin()
// // {
// //     // Wi-Fi AP is already up, just start peripherals & routes
// //     Mouse.begin();
// //     kbd.begin();
// //
// //     duckServer.on("/mouse" , HTTP_GET, handleMouse);
// //     duckServer.on("/key"   , HTTP_GET, handleKey);
// //     duckServer.on("/status", HTTP_GET, handleStatus);
// //     duckServer.begin();
// // }
