// duck_control_web.h  (very small)
#pragma once
#include <Arduino.h>
#include <ESPAsyncWebServer.h>

#include <USBHIDMouse.h>      // <-- new
#include <USBHIDKeyboard.h>   // already pulled in by <USB.h>, but explicit is fine
// extern USBHIDMouse    Mouse;      // relative mouse
// extern USBHIDKeyboard kbd;        // keyboard that main.cpp already uses
//
// USBHIDMouse    Mouse;          // NOTE the capital ‘M’
// USBHIDKeyboard kbd;

namespace duck_api {
    void attach(AsyncWebServer &srv);   // hook routes into the existing server
}


void duck_control_web_begin();

/*
extern AsyncWebServer  server;
extern USBHIDMouse     hidMouse;   //  ✱ new ✱ one global instance we reuse

void handleMouse  (AsyncWebServerRequest *request);
void handleKey    (AsyncWebServerRequest *request);
void handleStatus (AsyncWebServerRequest *request);*/
