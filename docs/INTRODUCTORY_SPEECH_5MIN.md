# Nema Kiosk Robot — 5-Minute Presentation Script (Simple English)

**Robot Name:** Nema  
**Target Duration:** 5 Minutes (~650 spoken words @ 130 wpm)  
**Tone:** Plain, clear, simple conversational English  
**File Location:** `docs/INTRODUCTORY_SPEECH_5MIN.md`  

---

## ⏱️ Speech Schedule & Timing

```
[0:00 - 1:00]  PART 1: Introduction, Meet Nema, & What it Does
[1:00 - 2:15]  PART 2: The Brain, the Muscles, & the Dedicated Mic Node
[2:15 - 3:30]  PART 3: Software division (Frontend vs Backend) & Local AI
[3:30 - 4:15]  PART 4: 3D Model & Our 3 Custom PCBs
[4:15 - 5:00]  PART 5: Benchmarks & Closing
```

---

## 🎙️ Spoken Speech Script (Verbatim)

### PART 1: Introduction, Meet Nema, & What it Does (0:00 – 1:00)

> "Hello everyone! Today, I am very excited to introduce **Nema**, our interactive campus kiosk robot.
> 
> Nema is designed to be a friendly, welcoming robot for visitors on our campus. It does three main things:
> 
> First, it is a **greeting robot**. When you walk up to it, Nema detects your presence, looks right at you, and greets you.
> Second, it acts as an **information kiosk**. You can ask Nema about happening events on campus.
> Third, it helps you find your way around by showing you **how to navigate to faculty places**.
> 
> Instead of using a boring, static touchscreen, students can talk directly to Nema and get help in a natural, friendly way."

---

### PART 2: The Brain, the Muscles, & the Dedicated Mic Node (1:00 – 2:15)

> "To make Nema work smoothly, we split the hardware compute into three main parts:
> 
> First, we use a **Raspberry Pi 4** as the main brain. It runs our face tracking code, decides Nema's emotions, and handles the voice AI.
> 
> Second, we use an **ESP32 microcontroller** to control the physical hardware. It acts like the muscles, directly moving the servos for the head pan-tilt, controling the arm, and spinning the base motor.
> 
> Third, we have a **Raspberry Pi Zero 2 W** running as a dedicated microphone module. Usually, running round screen eyes on the Pi 4 conflicts with digital mic wiring. By using the Pi Zero 2 W just for the microphone, we stopped all wiring clashes and isolated the audio from motor electrical noise."

---

### PART 3: Software division (Frontend vs Backend) & Local AI (2:15 – 3:30)

> "Our software is split into two major parts: the **Backend** and the **Frontend**.
> 
> The **Backend** runs in Python and controls all the physical robot functions. It captures camera video to perform face tracking, changes Nema’s emotions, and talks to the ESP32 to move the motors.
> 
> The **Frontend** is a Next.js touchscreen interface. It is what users see and touch on the kiosk. It displays the calendar of events, interactive maps to faculty locations, and live voice visualizers.
> 
> To answer questions instantly without awkward internet lag, we pre-save voice answers on the Pi. When a student asks a question, the backend matches it using local vector search (`all-MiniLM-L6-v2`) and plays the voice file **instantly—in under 40 milliseconds**!"

---

### PART 4: 3D Model & Our 3 Custom PCBs (3:30 – 4:15)

> "Nema is built using a custom 3D-designed robot body. To make the electronics clean, stable, and professional, we designed **three custom Printed Circuit Boards (PCBs)**:
> 
> The first PCB is for the **Raspberry Pi 4**. It plugs directly into the Pi 4 and handles the connections for the two round SPI display eyes and the head IMU sensor.
> 
> The second PCB is for the **ESP32**. It mounts the ESP32 and connects all the hardware controls—including the servos, the base motor driver, the encoders, and the laser distance sensors.
> 
> The third PCB is for the **Raspberry Pi Zero 2 W**. It mounts the Pi Zero and its digital INMP441 MEMS microphone module so we get clean, clear voice capture.
> 
> These custom boards eliminate messy wiring and make the hardware extremely reliable."

---

### PART 5: Benchmarks & Closing (4:15 – 5:00)

> "In our tests, Nema performs incredibly well:
> - Face tracking runs smoothly at **20 frames per second**.
> - Physical motor adjustments run at **50 updates per second**.
> - Local voice matching responds in **less than 40 milliseconds**.
> - The 3 custom PCBs keep the wiring clean and secure.
> 
> In conclusion, Nema proves that we can build a fast, smart, and physically interactive kiosk robot using smart software division, custom electronics, and low-cost hardware.
> 
> Thank you so much! I am happy to answer any questions."

---

## 📌 Quick Summary Card (Cheat Sheet)

| Topic | Simple Explanation |
| :--- | :--- |
| **Robot Name** | **Nema** (Friendly interactive greeting, event, and navigation kiosk robot) |
| **Pi 4 Brain PCB** | Custom PCB hosting Pi 4, dual round SPI screen eyes, and head IMU sensor |
| **ESP32 Muscle PCB** | Custom PCB hosting ESP32 and motor/servo/encoder/ToF sensor controls |
| **Pi Zero Mic PCB** | Custom PCB hosting Pi Zero 2 W and digital INMP441 MEMS microphone |
| **Backend Role** | Face tracking, showing emotions, and hardware communication controls |
| **Frontend Role** | Touchscreen display showing campus events, interactive maps, and waveforms |
| **NLU Matcher** | Local vector search (`all-MiniLM-L6-v2`) playing audio in $<40\text{ ms}$ |
