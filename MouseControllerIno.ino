// USB
#include <usbhub.h>
USB     Usb;
USBHub     Hub(&Usb);

// Human Interface Device — one HIDBoot instance per mouse
#include <hidboot.h>
HIDBoot<USB_HID_PROTOCOL_MOUSE>    HidMouseTop(&Usb);
HIDBoot<USB_HID_PROTOCOL_MOUSE>    HidMouseBottom(&Usb);

// SPI
#include <SPI.h>

// Per-mouse accumulated motion (maps to v_top / v_bottom in ADNS3080 V5)
struct MouseAccum {
  int dx;
  int dy;
  int X;
  int Y;
};

MouseAccum mouse_top = {0, 0, 0, 0};
MouseAccum mouse_bottom = {0, 0, 0, 0};

// Variables to convert x,y readings into spherical treadmill motion estimates
int8_t yaw, pitch, roll;
float yawFloat, pitchFloat, rollFloat;
float back_scale = 1; // should be 0.1?
float scale_ratio = 1;
float bottom_scale = back_scale * scale_ratio;
float send_period;
float send_rate = 60; // Hz
unsigned long last_send_millis;

// ----- Mouse Report Parser (one instance per USB mouse)
class MouseRptParser : public MouseReportParser
{
  MouseAccum *accum;
  public:
    MouseRptParser(MouseAccum *a) : accum(a) {}
  protected:
    void OnMouseMove  (MOUSEINFO *mi);
    void OnLeftButtonUp (MOUSEINFO *mi);
    void OnLeftButtonDown (MOUSEINFO *mi);
    void OnRightButtonUp  (MOUSEINFO *mi);
    void OnRightButtonDown  (MOUSEINFO *mi);
    void OnMiddleButtonUp (MOUSEINFO *mi);
    void OnMiddleButtonDown (MOUSEINFO *mi);
};
void MouseRptParser::OnMouseMove(MOUSEINFO *mi)
{
  accum->dx = mi->dX;
  accum->dy = mi->dY;
  accum->X += mi->dX;
  accum->Y += mi->dY;
};
void MouseRptParser::OnLeftButtonUp (MOUSEINFO *mi)
{
  Serial.println("L Butt Up");
};
void MouseRptParser::OnLeftButtonDown (MOUSEINFO *mi)
{
  Serial.println("L Butt Dn");
};
void MouseRptParser::OnRightButtonUp  (MOUSEINFO *mi)
{
  Serial.println("R Butt Up");
};
void MouseRptParser::OnRightButtonDown  (MOUSEINFO *mi)
{
  Serial.println("R Butt Dn");
};
void MouseRptParser::OnMiddleButtonUp (MOUSEINFO *mi)
{
  Serial.println("M Butt Up");
};
void MouseRptParser::OnMiddleButtonDown (MOUSEINFO *mi)
{
  Serial.println("M Butt Dn");
};

MouseRptParser PrsTop(&mouse_top);
MouseRptParser PrsBottom(&mouse_bottom);

// -----------------
//  setup()
// -----------------
void setup()
{
  last_send_millis = millis();
  send_period = (1000 / send_rate);

  Serial.begin( 115200 );

  if (Usb.Init() == -1)
    Serial.println("OSC did not start.");

  delay(200);

  HidMouseTop.SetReportParser(0, &PrsTop);
  HidMouseBottom.SetReportParser(0, &PrsBottom);

  mouse_top.X = 0;
  mouse_top.Y = 0;
  mouse_bottom.X = 0;
  mouse_bottom.Y = 0;

  Serial.println("Mice Ready (top + bottom)");
}

// -----------------
//  loop()
// -----------------
void loop()
{
  Usb.Task();

  if ((millis() - last_send_millis) >= send_period) {
    last_send_millis += send_period;

    // Same formulas as ADNS3080_Mouse_Controller_V5
    yawFloat = float(mouse_bottom.X) * bottom_scale;
    pitchFloat = float(mouse_top.X) * back_scale;
    rollFloat = 0.5 * (float(mouse_bottom.Y) * bottom_scale + float(mouse_top.Y) * back_scale);
    yaw = int8_t(yawFloat);
    pitch = int8_t(pitchFloat);
    roll = int8_t(rollFloat);

    Serial.print("bottom X=");
    Serial.print(mouse_bottom.X);
    Serial.print(" Y=");
    Serial.print(mouse_bottom.Y);
    Serial.print(" | top X=");
    Serial.print(mouse_top.X);
    Serial.print(" Y=");
    Serial.print(mouse_top.Y);
    Serial.print(" | yaw=");
    Serial.print(yawFloat);
    Serial.print(" pitch=");
    Serial.print(pitchFloat);
    Serial.print(" roll=");
    Serial.println(rollFloat);

    mouse_top.X = 0;
    mouse_top.Y = 0;
    mouse_bottom.X = 0;
    mouse_bottom.Y = 0;
  }
}
