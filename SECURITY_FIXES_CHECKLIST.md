# Security Fixes Checklist - HID_Tunnel

Quick reference guide for developers to track security fix progress.

---

## 🔴 CRITICAL - Must Fix Immediately

### C++ Buffer Overflows (RCE Risk)

- [ ] **CPP-001**: Fix mDNS buffer overflow (line 101)
  ```cpp
  // Current (VULNERABLE):
  buffer[len] = '\0';

  // Fix:
  if (len <= 0 || len >= sizeof(buffer)) return;
  buffer[len] = '\0';
  ```

- [ ] **CPP-002**: Fix MQTT payload buffer overflow (line 339)
  ```cpp
  // Ensure payload buffer is sized correctly
  char payload_buffer[512];
  if (len >= sizeof(payload_buffer)) return;
  memcpy(payload_buffer, payload, len);
  payload_buffer[len] = '\0';
  ```

- [ ] **DESIGN-006**: Fix same issue in v4 (line 339)

### Thread Safety (Crash Risk)

- [ ] **CPP-003**: Add mutex to discoveredEndpoints vector
  ```cpp
  // Add member:
  std::mutex endpoints_mutex;

  // Wrap all access:
  std::lock_guard<std::mutex> lock(endpoints_mutex);
  for (auto& ep : discoveredEndpoints) { ... }
  ```

- [ ] **PY-002**: Fix recursive thread creation in reconnection
  ```python
  # Replace recursive pattern with:
  def reconnect_worker():
      while not self.shutdown_flag.is_set():
          time.sleep(self.reconnect_delays[broker_key])
          if not client.is_connected():
              try:
                  client.reconnect()
                  break  # Success
              except:
                  delay = min(delay * 2, self.max_reconnect_delay)
                  self.reconnect_delays[broker_key] = delay
  ```

- [ ] **PY-015**: Apply same fix to v4

### Blocking Operations (DoS Risk)

- [ ] **CPP-004**: Move HTTP polling to separate task
  ```cpp
  // Create separate task:
  xTaskCreate(httpPollTask, "HTTP_Poll", 4096, NULL, 1, &httpPollTaskHandle);

  // In task:
  void httpPollTask(void* param) {
      while(1) {
          httpPoll();
          vTaskDelay(pdMS_TO_TICKS(HTTP_POLL_INTERVAL_MS));
      }
  }
  ```

---

## 🟠 HIGH SEVERITY - Fix Before Release

### Authentication & Authorization

- [ ] **DESIGN-003**: Add authentication to mDNS
  ```cpp
  // Add HMAC verification:
  String hmac_received = doc["hmac"];
  String computed_hmac = computeHMAC(message, shared_secret);
  if (hmac_received != computed_hmac) return;
  ```

### Resource Exhaustion

- [ ] **PY-004**: Add queue size limit
  ```python
  self.pending_commands = queue.Queue(maxsize=100)

  try:
      self.pending_commands.put(command, timeout=0.1)
  except queue.Full:
      # Drop oldest
      try:
          self.pending_commands.get_nowait()
          self.pending_commands.put(command)
      except:
          pass
  ```

- [ ] **DESIGN-005**: Limit discoveredEndpoints size
  ```cpp
  const size_t MAX_DISCOVERED_ENDPOINTS = 10;

  if (discoveredEndpoints.size() >= MAX_DISCOVERED_ENDPOINTS) {
      // Remove oldest
      discoveredEndpoints.erase(discoveredEndpoints.begin());
  }
  ```

### Race Conditions

- [ ] **PY-003**: Fix MQTT client access atomicity
  ```python
  def send_mouse(self, command: dict):
      with self.lock:
          if self.active_broker and self.active_broker in self.clients:
              broker = self.active_broker
              client = self.clients[broker]
      # Publish outside lock with local references
      if broker and client:
          client.publish(self.mouse_topic, json.dumps(command), qos=0)
  ```

- [ ] **PY-005**: Fix mouse command race conditions
  ```python
  def send_mouse_command(self, ...):
      # Single lock acquisition for all operations
      with self.lock:
          if not self.active_transport:
              return
          transport = self.active_transport  # Copy reference
          # ... do all checks and operations ...
          transport.send_mouse(command)  # Use local reference
  ```

- [ ] **CPP-005**: Fix TOCTOU in endpoint selection
  ```cpp
  DiscoveredEndpoint ep_copy;
  bool found = false;
  {
      std::lock_guard<std::mutex> lock(endpoints_mutex);
      if (!discoveredEndpoints.empty()) {
          ep_copy = discoveredEndpoints[0];
          found = true;
      }
  }
  if (found) {
      wsClient.begin(ep_copy.host.c_str(), ep_copy.ws_port, "/");
  }
  ```

### Error Handling

- [ ] **PY-001**: Replace all bare except clauses
  ```python
  # Find all instances of:
  except:

  # Replace with:
  except Exception as e:
      logging.error(f"Error: {e}")
  ```
  Locations: Lines 46, 72, 78, 241, 286, 332, 361, 384, 466, 491, 614, 769, 835

### Input Validation

- [ ] **PY-006**: Fix URL parameter parsing
  ```python
  from urllib.parse import parse_qs, urlparse

  parsed = urlparse(path)
  params = parse_qs(parsed.query)

  try:
      dx = int(params.get('dx', [0])[0])
      dx = max(-127, min(127, dx))  # Clamp
  except (ValueError, IndexError):
      return b"Invalid parameter"
  ```

- [ ] **PY-013**: Validate Content-Length
  ```python
  MAX_CONTENT_LENGTH = 4096
  length = int(self.headers.get('Content-Length', 0))
  if length > MAX_CONTENT_LENGTH:
      self.send_error(413, "Payload too large")
      return
  ```

- [ ] **PY-014**: Add JSON schema validation
  ```python
  from jsonschema import validate, ValidationError

  MOUSE_SCHEMA = {
      "type": "object",
      "properties": {
          "dx": {"type": "integer", "minimum": -127, "maximum": 127},
          "dy": {"type": "integer", "minimum": -127, "maximum": 127},
          # ...
      }
  }

  try:
      validate(instance=payload, schema=MOUSE_SCHEMA)
  except ValidationError as e:
      logging.error(f"Invalid message: {e}")
      return
  ```

---

## 🟡 MEDIUM SEVERITY - Fix Soon

### Design Issues

- [ ] **PY-007**: Fix asyncio event loop conflicts
  ```python
  # Store event loop reference during WebSocket setup
  self.event_loop = asyncio.get_event_loop()

  # Use in send:
  future = asyncio.run_coroutine_threadsafe(
      self.client_ws.send(json.dumps(data)),
      self.event_loop
  )
  ```

- [ ] **PY-008**: Coordinate timeout handlers
  ```python
  # Use single background thread with event coordination
  self.timeout_event = threading.Event()

  def background_worker(self):
      while not self.shutdown_flag.is_set():
          self.timeout_event.wait(timeout=0.5)
          # Coordinated timeout and discovery logic
  ```

- [ ] **DESIGN-001**: Fix HTTP connection timing
  ```python
  # Adjust timeout to match actual timing:
  HTTP_CONNECTION_TIMEOUT = 30  # poll_interval(2) + timeout(25) + margin(3)

  return self.connected and (time.time() - self.last_poll_time < HTTP_CONNECTION_TIMEOUT)
  ```

- [ ] **DESIGN-002**: Improve transport switching logic
  ```cpp
  // Track per-transport success metrics
  struct TransportMetrics {
      uint32_t success_count;
      uint32_t failure_count;
      unsigned long last_success_ms;
  };

  // Only switch after repeated failures
  if (metrics.failure_count > 3 && metrics.success_count == 0) {
      switchTransport();
  }
  ```

### Security

- [ ] **PY-009**: Fix CORS policy
  ```python
  ALLOWED_ORIGINS = ["http://localhost:8080"]

  origin = self.headers.get('Origin', '')
  if origin in ALLOWED_ORIGINS:
      self.send_header("Access-Control-Allow-Origin", origin)
  ```

- [ ] **PY-010**: Secure mDNS broadcast
  ```python
  # Add HMAC to announcement
  import hmac
  import hashlib

  announcement['hmac'] = hmac.new(
      shared_secret.encode(),
      json.dumps({k:v for k,v in announcement.items() if k != 'hmac'}).encode(),
      hashlib.sha256
  ).hexdigest()
  ```

- [ ] **INFO-002**: Secure status messages
  ```cpp
  // Only include sensitive info if authenticated
  if (authenticated) {
      doc["free_heap"] = ESP.getFreeHeap();
      doc["uptime_ms"] = millis();
  }
  ```

### Resource Management

- [ ] **PY-012**: Fix socket leak
  ```python
  # Use context manager:
  try:
      with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
          s.connect(("8.8.8.8", 80))
          local_ip = s.getsockname()[0]
          return local_ip
  except Exception as e:
      logging.error(f"Failed to get local IP: {e}")
      return "127.0.0.1"
  ```

---

## 🟢 LOW SEVERITY - Nice to Have

### Code Quality

- [ ] **DESIGN-004**: Remove hardcoded IPs
  ```cpp
  // Use configuration file or WiFi manager
  // Remove all instances of 192.168.1.100
  ```

- [ ] **INFO-001**: Add quiet mode
  ```python
  ap.add_argument("--quiet", action="store_true", help="Reduce output")

  if not args.quiet:
      print(f"Local IP: {local_ip}")
  ```

### Performance

- [ ] **PERF-001**: Optimize mouse delta accumulation
  ```python
  # Remove redundant while loop - already clamped
  step_x = max(-127, min(127, dx))
  step_y = max(-127, min(127, dy))
  step_w = max(-127, min(127, wheel))
  api_get(base, f"/mouse?dx={step_x}&dy={step_y}&wheel={step_w}", dbg)
  ```

- [ ] **PERF-002**: Normalize mouse sensitivity
  ```python
  # Make configurable
  MOUSE_SCALING = args.mouse_scale if hasattr(args, 'mouse_scale') else 1.0
  dx += (x - last_xy[0]) * MOUSE_SCALING
  ```

- [ ] **PERF-003**: Optimize mDNS processing
  ```cpp
  // Check availability first
  if (mdnsUdp.available() > 0) {
      processMdnsAnnouncement();
  }
  ```

---

## Testing Checklist

### Security Tests

- [ ] Fuzz test mDNS parser with random UDP packets
- [ ] Fuzz test JSON parsers with malformed input
- [ ] Test buffer overflows with boundary values
- [ ] Test authentication bypass attempts
- [ ] Verify CORS policy enforcement
- [ ] Test rate limiting under load

### Reliability Tests

- [ ] Stress test with 1000+ messages/second
- [ ] Test reconnection under network failures
- [ ] Verify no memory leaks over 24-hour run
- [ ] Test thread safety with ThreadSanitizer
- [ ] Test resource limits (max queue size, etc.)
- [ ] Verify graceful handling of all error paths

### Regression Tests

- [ ] Unit test all input parsers
- [ ] Test transport switching logic
- [ ] Test state machine transitions
- [ ] Verify keyboard state consistency
- [ ] Test concurrent access patterns
- [ ] Verify timeout mechanisms

---

## Verification Commands

### Build with Sanitizers
```bash
# Python: Run with warnings
python -W all HID_remote_v5.py

# C++: Build with sanitizers
pio run -t clean
export CPPFLAGS="-fsanitize=address,undefined -g"
pio run
```

### Static Analysis
```bash
# Python
pylint HID_remote_v5.py
bandit -r .
mypy HID_remote_v5.py

# C++
cppcheck --enable=all UltraWiFiDuck/src/
clang-tidy UltraWiFiDuck/src/*.cpp
```

### Security Scanning
```bash
# Dependency scanning
pip-audit
safety check

# Secret scanning
git secrets --scan
trufflehog filesystem .
```

---

## Progress Tracking

**Last Updated:** 2025-11-14

| Category | Fixed | Total | %Complete |
|----------|-------|-------|-----------|
| 🔴 Critical | 0 | 23 | 0% |
| 🟠 High | 0 | 31 | 0% |
| 🟡 Medium | 0 | 28 | 0% |
| 🟢 Low | 0 | 15 | 0% |
| **Total** | **0** | **97** | **0%** |

---

## Sign-off

After all critical and high severity issues are fixed:

- [ ] Security team review completed
- [ ] Penetration testing passed
- [ ] Code review approved
- [ ] Documentation updated
- [ ] Release notes prepared

**Approved by:** _______________
**Date:** _______________
**Version:** _______________
