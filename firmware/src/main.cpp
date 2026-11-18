// StickBuddy firmware - PTT + on-device status display
// =====================================================
// Button edge events go out over BLE (custom GATT), all gesture logic on PC.
// Status comes back over RX channel (ST:REC/THINK/DONE/ERR/IDLE) and is shown
// on the ST7789 LCD. Screen power is gated by the M5PM1 PMIC's PYG2 output
// (I2C addr 0x6E, SDA=G47/SCL=G48, standard-mode 100kHz ONLY).
//
// Screen states (no font, shapes + colors only):
//   REC   : black bg + big red dot        (recording)
//   THINK : black bg + blinking amber dot (transcribing, 30s watchdog)
//   DONE  : black bg + green checkmark    (shown ~1.2s, then backlight off)
//   ERR   : black bg + red X              (shown ~1.2s, then backlight off)
//   OFF   : backlight off

#include <Arduino.h>
#include <Wire.h>
#include <SPI.h>
#include <math.h>
#include <NimBLEDevice.h>
#include "font24.h"

#define FW_VERSION "stickbuddy-1.5"

static const char *SERVICE_UUID = "6b1e0001-5f2a-4c8e-9d3b-1a2b3c4d5e6f";
static const char *TX_UUID = "6b1e0002-5f2a-4c8e-9d3b-1a2b3c4d5e6f";
static const char *RX_UUID = "6b1e0003-5f2a-4c8e-9d3b-1a2b3c4d5e6f";

static const int PIN_FRONT = 11;
static const int PIN_SIDE = 12;
static const uint32_t DEBOUNCE_MS = 25;

// ---------- LCD pins (M5StickS3, per docs.m5stack.com) ----------
#define LCD_MOSI 39
#define LCD_SCLK 40
#define LCD_CS   41
#define LCD_DC   45
#define LCD_RST  21
#define LCD_BL   38
#define LCD_W    135
#define LCD_H    240
#define LCD_XOFF 52     // 135x240 window inside 240x240 controller
#define LCD_YOFF 40

// ---------- colors (RGB565) ----------
#define C_BLACK 0x0000
#define C_RED   0xF800
#define C_AMBER 0xFD20
#define C_GREEN 0x07E0
#define C_BLUE  0x001F

// ---------- M5PM1 registers (ref: Blockstream Jade m5pm1 driver) ----------
#define PM1_ADDR           0x6E
#define PM1_REG_DEVICE_ID  0x00   // expect 0x50
#define PM1_REG_I2C_CFG    0x09   // [4]SPD(1=400k) [3:0]idle-sleep timeout; write 0x00
#define PM1_REG_WDT_CNT    0x0A   // watchdog seconds; write 0x00 = off
#define PM1_REG_GPIO_MODE  0x10   // [4:0] direction, 1=output
#define PM1_REG_GPIO_DRV   0x13   // [4:0] 1=open-drain 0=push-pull
#define PM1_REG_GPIO_FUNC0 0x16   // 2 bits per GPIO0..3; 0x00 = plain GPIO
#define PM1_REG_GPIO_OUT   0x11   // [4:0] output level
#define PM1_PYG2 2                // gates L3B rail = LCD backlight/mic/spk power, ACTIVE HIGH
#define PM1_PYG3 3                // speaker amp, keep LOW

NimBLECharacteristic *txChar = nullptr;
volatile bool bleConnected = false;

// ===================== M5PM1 =====================
static uint8_t pm1_write(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(PM1_ADDR);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission();
}

static int pm1_read(uint8_t reg) {
  Wire.beginTransmission(PM1_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return -1;
  int n = Wire.requestFrom((int)PM1_ADDR, 1);
  if (n != 1) return -1;
  return Wire.read();
}

static void pm1_init() {
  // PMIC is a 100kHz standard-mode device; 400kHz gets NAK'd.
  Wire.begin(47, 48);
  Wire.setClock(100000);
  delay(10);

  // Disable PMIC i2c idle-sleep and watchdog so it never drops rails under us.
  pm1_write(PM1_REG_I2C_CFG, 0x00);
  pm1_write(PM1_REG_WDT_CNT, 0x00);
  // PYG2/PYG3 as push-pull plain-GPIO outputs.
  pm1_write(PM1_REG_GPIO_FUNC0, 0x00);
  pm1_write(PM1_REG_GPIO_DRV, 0x00);
  pm1_write(PM1_REG_GPIO_MODE, (1 << PM1_PYG2) | (1 << PM1_PYG3));
  // PYG2 HIGH powers the LCD rail, PYG3 LOW keeps the speaker amp off.
  pm1_write(PM1_REG_GPIO_OUT, 1 << PM1_PYG2);

  int id  = pm1_read(PM1_REG_DEVICE_ID);
  int out = pm1_read(PM1_REG_GPIO_OUT);
  Serial.printf("[pm1] device_id=0x%02X gpio_out=0x%02X (expect 0x50 / 0x04)\n",
                id & 0xFF, out & 0xFF);
}

// ===================== ST7789 LCD =====================
static inline void lcd_cs(bool high) { digitalWrite(LCD_CS, high ? HIGH : LOW); }
static inline void lcd_dc(bool high) { digitalWrite(LCD_DC, high ? HIGH : LOW); }

static void lcd_cmd(uint8_t c) {
  lcd_cs(false); lcd_dc(false);
  SPI.write(c);
  lcd_cs(true);
}

static void lcd_data(const uint8_t *d, size_t n) {
  lcd_cs(false); lcd_dc(true);
  if (n) SPI.writeBytes(d, n);
  lcd_cs(true);
}

static void lcd_data1(uint8_t v) { lcd_data(&v, 1); }

static void lcd_window(uint16_t x, uint16_t y, uint16_t w, uint16_t h) {
  uint16_t x0 = x + LCD_XOFF, x1 = x + w - 1 + LCD_XOFF;
  uint16_t y0 = y + LCD_YOFF, y1 = y + h - 1 + LCD_YOFF;
  uint8_t ca[4] = {(uint8_t)(x0 >> 8), (uint8_t)(x0 & 0xFF), (uint8_t)(x1 >> 8), (uint8_t)(x1 & 0xFF)};
  uint8_t ra[4] = {(uint8_t)(y0 >> 8), (uint8_t)(y0 & 0xFF), (uint8_t)(y1 >> 8), (uint8_t)(y1 & 0xFF)};
  lcd_cmd(0x2A); lcd_data(ca, 4);   // CASET
  lcd_cmd(0x2B); lcd_data(ra, 4);   // RASET
  lcd_cmd(0x2C);                    // RAMWR
}

static uint16_t g_linebuf[LCD_W];

static void lcd_fill_rect(uint16_t x, uint16_t y, uint16_t w, uint16_t h, uint16_t color) {
  if (!w || !h) return;
  lcd_window(x, y, w, h);
  // ESP32 是小端序，ST7789 要求每像素高字节先发（MSB first），
  // 不交换字节的话 0xF800(红) 会以 0x00F8(青蓝) 发出去 —— 红/蓝错乱。
  for (uint16_t i = 0; i < w; i++) g_linebuf[i] = __builtin_bswap16(color);
  lcd_cs(false); lcd_dc(true);
  for (uint16_t j = 0; j < h; j++) SPI.writeBytes((uint8_t *)g_linebuf, w * 2);
  lcd_cs(true);
}

static void lcd_fill_screen(uint16_t color) { lcd_fill_rect(0, 0, LCD_W, LCD_H, color); }

// 逐行直接写入任意像素颜色（文字渲染用），内部做字节交换
static void lcd_blit_line(uint16_t x, uint16_t y, uint16_t w, const uint16_t *px) {
  lcd_window(x, y, w, 1);
  for (uint16_t i = 0; i < w; i++) g_linebuf[i] = __builtin_bswap16(px[i]);
  lcd_cs(false); lcd_dc(true);
  SPI.writeBytes((uint8_t *)g_linebuf, w * 2);
  lcd_cs(true);
}

static void lcd_fill_circle(int cx, int cy, int r, uint16_t color) {
  for (int dy = -r; dy <= r; dy++) {
    int hw = (int)sqrtf((float)(r * r - dy * dy));
    lcd_fill_rect(cx - hw, cy + dy, hw * 2 + 1, 1, color);
  }
}

// "blob line": thick line made of overlapping filled circles
static void lcd_blob_line(int x0, int y0, int x1, int y1, int r, uint16_t color) {
  int dist = max(abs(x1 - x0), abs(y1 - y0));
  int steps = max(1, dist / max(1, r / 2));
  for (int i = 0; i <= steps; i++) {
    int x = x0 + (x1 - x0) * i / steps;
    int y = y0 + (y1 - y0) * i / steps;
    lcd_fill_circle(x, y, r, color);
  }
}

static void backlight(bool on) { digitalWrite(LCD_BL, on ? HIGH : LOW); }

static void lcd_init() {
  pinMode(LCD_CS, OUTPUT);
  pinMode(LCD_DC, OUTPUT);
  pinMode(LCD_RST, OUTPUT);
  pinMode(LCD_BL, OUTPUT);
  digitalWrite(LCD_CS, HIGH);
  digitalWrite(LCD_BL, LOW);
  SPI.begin(LCD_SCLK, -1, LCD_MOSI, LCD_CS);

  digitalWrite(LCD_RST, HIGH); delay(10);
  digitalWrite(LCD_RST, LOW);  delay(10);
  digitalWrite(LCD_RST, HIGH); delay(120);

  SPI.beginTransaction(SPISettings(40000000, MSBFIRST, SPI_MODE0));
  lcd_cmd(0x01); delay(150);                       // SWRESET
  lcd_cmd(0x11); delay(120);                       // SLPOUT
  lcd_cmd(0x3A); lcd_data1(0x55);                  // 16-bit color
  lcd_cmd(0x36); lcd_data1(0x00);                  // MADCTL portrait
  lcd_cmd(0xB2); { const uint8_t d[] = {0x0C,0x0C,0x00,0x33,0x33}; lcd_data(d, 5); }  // PORCH
  lcd_cmd(0xB7); lcd_data1(0x14);                  // GCTRL
  lcd_cmd(0xBB); lcd_data1(0x37);                  // VCOMS
  lcd_cmd(0xC0); lcd_data1(0x2C);                  // LCMCTRL
  lcd_cmd(0xC2); lcd_data1(0x01);                  // VDV/VRH enable
  lcd_cmd(0xC3); lcd_data1(0x12);                  // VRHS
  lcd_cmd(0xC4); lcd_data1(0x20);                  // VDV
  lcd_cmd(0xC6); lcd_data1(0x0F);                  // FRCTRL2 ~60Hz
  lcd_cmd(0xD0); { const uint8_t d[] = {0xA4, 0xA1}; lcd_data(d, 2); }  // PWRCTRL
  lcd_cmd(0x21);                                   // INVON (IPS panel)
  lcd_cmd(0x29); delay(50);                        // DISPON
  lcd_fill_screen(C_BLACK);
  Serial.println("[lcd] init done");
}

// ===================== UI state machine (text based) =====================
enum UiState : uint8_t { UI_OFF = 0, UI_REC, UI_THINK, UI_DONE, UI_ERR, UI_WB_BUSY, UI_WB_DONE, UI_HELLO };

static volatile uint8_t uiPending = 255;   // 255 = nothing pending (set from BLE thread)
static uint8_t  uiState  = 255;            // force first paint
static uint32_t uiDeadline = 0;            // transient state auto-revert time
static uint16_t g_textline[LCD_W];

// ---- 24x24 点阵文字（中文自造字模，不依赖字体库） ----
static const uint8_t *glyph_of(uint16_t cp) {
  for (int i = 0; i < GLYPH_N; i++)
    if (GLYPHS[i].cp == cp) return GLYPHS[i].bits;
  return nullptr;
}

static int utf8_decode(const char *s, uint16_t *cp) {
  const uint8_t *p = (const uint8_t *)s;
  if (p[0] < 0x80)               { *cp = p[0]; return 1; }
  if ((p[0] & 0xE0) == 0xC0)     { *cp = ((p[0] & 0x1F) << 6) | (p[1] & 0x3F); return 2; }
  if ((p[0] & 0xF0) == 0xE0)     { *cp = ((p[0] & 0x0F) << 12) | ((p[1] & 0x3F) << 6) | (p[2] & 0x3F); return 3; }
  *cp = '?'; return 1;
}

static void ui_draw_text(const char *txt, uint16_t color) {
  const uint8_t *gs[8];
  int gn = 0, i = 0;
  uint16_t cp;
  while (txt[i] && gn < 8) {
    i += utf8_decode(txt + i, &cp);
    gs[gn++] = glyph_of(cp);
  }
  int x0 = (LCD_W - gn * 24) / 2;
  int y0 = (LCD_H - 24) / 2;
  for (int r = 0; r < 24; r++) {
    for (int x = 0; x < LCD_W; x++) g_textline[x] = C_BLACK;
    for (int k = 0; k < gn; k++) {
      const uint8_t *g = gs[k];
      if (!g) continue;
      for (int c = 0; c < 24; c++) {
        if (g[r * 3 + c / 8] & (0x80 >> (c % 8))) {
          int xx = x0 + k * 24 + c;
          if (xx >= 0 && xx < LCD_W) g_textline[xx] = color;
        }
      }
    }
    lcd_blit_line(0, y0 + r, LCD_W, g_textline);
  }
}

static void ui_show(const char *txt, uint16_t color, uint32_t ms) {
  backlight(true);
  lcd_fill_screen(C_BLACK);
  ui_draw_text(txt, color);
  uiDeadline = ms ? millis() + ms : 0;
}

static void ui_apply(uint8_t st) {
  if (uiState == st) return;
  uiState = st;
  switch (st) {
    case UI_OFF:     backlight(false); break;
    case UI_REC:     ui_show("录音中", C_RED, 0); break;
    case UI_THINK:   ui_show("转写中", C_AMBER, 30000); break;    // PC 挂了也不会一直亮
    case UI_DONE:    ui_show("已完成", C_GREEN, 1500); break;
    case UI_ERR:     ui_show("出错", C_RED, 1500); break;
    case UI_WB_BUSY: ui_show("处理中", C_BLUE, 600000); break;    // WorkBuddy 任务，10 分钟看门狗
    case UI_WB_DONE: ui_show("任务完成", C_GREEN, 2500); break;
    case UI_HELLO:   ui_show("已连接", C_GREEN, 1500); break;
  }
}

static void ui_loop() {
  uint8_t pend = uiPending;
  if (pend != 255) {
    uiPending = 255;
    ui_apply(pend);
  }
  if (uiDeadline && millis() > uiDeadline) ui_apply(UI_OFF);
}

// ===================== BLE =====================
struct BtnState {
  const char *id;
  int pin;
  bool stable;
  bool rawLast;
  uint32_t rawChangeAt;
  uint32_t pressedAt;
};

BtnState frontBtn = {"F", PIN_FRONT, false, false, 0, 0};
BtnState sideBtn = {"S", PIN_SIDE, false, false, 0, 0};

void sendLine(const String &s) {
  Serial.printf("[tx] %s\n", s.c_str());
  if (!bleConnected || txChar == nullptr) return;
  txChar->setValue((uint8_t *)s.c_str(), s.length());
  txChar->notify();
}

class ServerCallbacks : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer *pServer) override {
    bleConnected = true;
    Serial.println("[ble] host connected");
    delay(30);
    sendLine(String("HELLO ") + FW_VERSION);
    uiPending = UI_HELLO;   // 连接上时屏幕显示「已连接」
  }
  void onDisconnect(NimBLEServer *pServer) override {
    bleConnected = false;
    Serial.println("[ble] host disconnected");
    uiPending = UI_OFF;
    NimBLEDevice::startAdvertising();
  }
};

class RxCallbacks : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic *c) override {
    String cmd(c->getValue().c_str());
    cmd.trim();
    Serial.printf("[rx] %s\n", cmd.c_str());
    // NOTE: runs in BLE stack thread - only set the pending flag, never draw here.
    if      (cmd == "ST:REC")    uiPending = UI_REC;
    else if (cmd == "ST:THINK")  uiPending = UI_THINK;
    else if (cmd == "ST:DONE")   uiPending = UI_DONE;
    else if (cmd == "ST:ERR")    uiPending = UI_ERR;
    else if (cmd == "ST:IDLE")   uiPending = UI_OFF;
    else if (cmd == "ST:WBBUSY") uiPending = UI_WB_BUSY;
    else if (cmd == "ST:WBDONE") uiPending = UI_WB_DONE;
    else if (cmd == "PING")      sendLine("PONG");
  }
};

void setupBle() {
  NimBLEDevice::init("StickBuddy");
  NimBLEDevice::setPower(ESP_PWR_LVL_P9);

  NimBLEServer *server = NimBLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());

  NimBLEService *svc = server->createService(SERVICE_UUID);
  txChar = svc->createCharacteristic(TX_UUID, NIMBLE_PROPERTY::NOTIFY);
  NimBLECharacteristic *rxChar = svc->createCharacteristic(
      RX_UUID, NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR);
  rxChar->setCallbacks(new RxCallbacks());
  svc->start();

  NimBLEAdvertising *adv = NimBLEDevice::getAdvertising();
  adv->addServiceUUID(SERVICE_UUID);
  adv->setScanResponse(true);
  adv->setMinPreferred(0x06);
  adv->start();
  Serial.println("[ble] advertising as StickBuddy");
}

void pollButton(BtnState &b) {
  bool raw = (digitalRead(b.pin) == LOW);
  uint32_t now = millis();
  if (raw != b.rawLast) {
    b.rawLast = raw;
    b.rawChangeAt = now;
  }
  if (now - b.rawChangeAt >= DEBOUNCE_MS && raw != b.stable) {
    b.stable = raw;
    if (raw) {
      b.pressedAt = now;
      sendLine(String(b.id) + ":1 " + String(now));
      // 丢掉按键前滞留的 BLE 状态（如旧绿勾指令），避免覆盖刚画的 REC/THINK
      if (b.pin == PIN_FRONT) { uiPending = 255; ui_apply(UI_REC); }
    } else {
      uint32_t held = now - b.pressedAt;
      sendLine(String(b.id) + ":0 " + String(now) + " held=" + String(held));
      // anticipate the PC: after release it transcribes -> THINK,
      // then PC overrides with DONE / ERR / IDLE. Clicks get DONE from PC too.
      if (b.pin == PIN_FRONT) { uiPending = 255; ui_apply(UI_THINK); }
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(50);
  pinMode(PIN_FRONT, INPUT_PULLUP);
  pinMode(PIN_SIDE, INPUT_PULLUP);
  Serial.printf("\n[boot] %s\n", FW_VERSION);
  pm1_init();
  lcd_init();
  setupBle();
}

void loop() {
  pollButton(frontBtn);
  pollButton(sideBtn);
  ui_loop();
  delay(5);
}
