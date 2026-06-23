import time
from hardware.arduino_servo import ArduinoServoLink

link = ArduinoServoLink()
if link.connect():
    print("Connected.")
    print("Sending pan/tilt/arms...")
    link.write_angles_and_arms(80.0, 110.0, 10.0, 170.0, 90.0, 90.0, force=True, wait_ack=True)
    time.sleep(1)
    
    # Read any output
    link._ser.timeout = 1.0
    print(link._ser.read_all().decode())
    link.close()
else:
    print("Failed to connect.")
