# Voice Agent V5 — 5-Minute Introductory Presentation & Speech Script

**Title:** Voice Agent V5: Multimodal Autonomous Kiosk Robot  
**Target Duration:** 5 Minutes (~700 spoken words @ 135 wpm)  
**Target Audience:** University Dean, Department Heads, Academic Evaluators, and Engineering Panel  
**Deliverable File:** `docs/INTRODUCTORY_SPEECH_5MIN.md`  

---

## ⏱️ Speech Delivery & Timing Map

```
[0:00 - 1:00]  PART 1: The Vision & Problem Statement (Hook)
[1:00 - 2:15]  PART 2: System Architecture & Distributed Compute
[2:15 - 3:30]  PART 3: Zero-Latency Multimodal AI & Vector NLU
[3:30 - 4:15]  PART 4: Engineering Innovations & Hardware Problem Solving
[4:15 - 5:00]  PART 5: Benchmarks, Real-World Impact & Closing
```

---

## 🎙️ Spoken Speech Script (Verbatim)

### PART 1: The Vision & Problem Statement (0:00 – 1:00)

> "Respected Dean, Members of the Faculty, and Distinguished Guests,
> 
> Imagine walking into our university building or a campus event hall, looking for a classroom or poster details, and instead of staring at a cold, static touchscreen display, you are greeted by an interactive, expressive robot. The robot turns its physical head to face you, greets you warmly, waves its robotic arm, and responds to your spoken questions in under 40 milliseconds with natural voice and synchronized screen graphics.
> 
> This is **Voice Agent V5** — an end-to-end multimodal autonomous kiosk and physical robotic platform that we have built from the ground up.
> 
> Traditional public kiosks fail because they are passive and cold. On the other hand, traditional humanoid robots fail because they are prohibitively expensive, suffer from high cloud latency, and quickly overheat on embedded hardware. Our goal with Voice Agent V5 was to solve this fundamental trade-off: creating a real-time, responsive, physically engaging robot built on low-cost, power-efficient hardware."

---

### PART 2: System Architecture & Distributed Compute (1:00 – 2:15)

> "To achieve real-time performance without lag or micro-stutters, we designed a heterogeneous, multi-tiered architecture.
> 
> At the core of the system is an overclocked **Raspberry Pi 4 Model B** running 64-bit Linux. The Pi 4 acts as the central brain — hosting our computer vision AI, natural language understanding engine, vector database, and Next.js 14 touchscreen kiosk user interface.
> 
> To ensure microsecond-level timing for physical motors without overloading the main CPU, we offloaded all actuator controls to an embedded **ESP32 microcontroller** over a custom high-speed 115,200 baud USB serial protocol. The ESP32 manages a PCA9685 12-bit PWM controller to drive the 2-DOF head pan-tilt servos and a 4-DOF articulated robotic arm, alongside a TB6612FNG motor driver with optical quadrature encoders for closed-loop 360-degree body spins.
> 
> Connecting all software modules is our centralized shared-memory bus — **The Blackboard Architecture**. By using atomic state locks and zero-CPU condition variables, six concurrent worker threads exchange real-time perception and kinematic data at zero idle CPU overhead."

---

### PART 3: Zero-Latency Multimodal AI & Vector NLU (2:15 – 3:30)

> "What truly makes Voice Agent V5 unique is its perception and zero-latency conversational intelligence.
> 
> For computer vision, we implemented OpenCV’s neural **YuNet face detector**, running on downsampled camera feeds with adaptive Region-of-Interest sub-windowing. When the user speaks, our system enters a zero-CPU speech pause mode, automatically throttling vision processing to preserve maximum CPU capacity for audio.
> 
> For voice, cloud-dependent speech systems often suffer from 2 to 3 seconds of latency. We solved this by building a **5-tier hybrid NLU pipeline**:
> 
> Before deployment, our automated poster compiler uses Vision LLMs to extract poster information, generates synthetic question variations, pre-synthesizes uncompressed 48kHz WAV audio files, and encodes them into 384-dimensional dense vector embeddings using the **`all-MiniLM-L6-v2`** model stored inside a local **ChromaDB vector store**.
> 
> At runtime, when a user asks a question, Silero VAD detects end-of-speech, and our system performs a cosine similarity search against ChromaDB. On a local vector match, the robot plays pre-cached 48kHz audio with **zero milliseconds of synthesis latency**! If an un-indexed question is asked, the system seamlessly falls back to cloud LLMs like Groq Llama-3 or local offline speech engines."

---

### PART 4: Engineering Innovations & Hardware Problem Solving (3:30 – 4:15)

> "Throughout development, we encountered and solved critical hardware constraints.
> 
> For instance, when driving dual hardware SPI buses on the Pi 4 to render animated TFT round eye graphics at 60 FPS, the required SPI pins physically collided with the Pi’s hardware I2S audio pins required by MEMS microphones. 
> 
> To resolve this without compromise, we engineered a dedicated **Pi Zero 2 W voice node** equipped with an INMP441 MEMS microphone. This physically and electrically isolated the microphone from motor PWM noise and eliminated all hardware pin collisions.
> 
> Furthermore, we implemented a **TCA9548A 8-channel I2C multiplexer** to operate three ST VL53L0X Time-of-Flight laser distance sensors simultaneously, giving the robot 3-zone spatial awareness to detect approaching users from up to 2.2 meters away."

---

### PART 5: Benchmarks, Real-World Impact & Closing (4:15 – 5:00)

> "In our empirical benchmarks on the Pi 4:
> - Vision runs at a smooth **18 to 24 Frames Per Second**.
> - Servo control loops operate deterministically at **50 Hz**.
> - Local vector voice queries respond in **less than 40 milliseconds**.
> - Peak CPU load remains safely below 78% with zero thermal throttling.
> 
> In conclusion, Voice Agent V5 proves that high-performance, zero-latency multimodal robotics does not require tens of thousands of dollars in high-end industrial hardware. Through intelligent software decoupling, vector pre-indexing, and optimized embedded system design, we have created a scalable, engaging platform for campus navigation and human-robot interaction.
> 
> Thank you, and I welcome any questions from the panel."

---

## 📌 Presentation Quick-Reference Card (Cheat Sheet)

| Topic | Key Metric / Detail to Remember |
| :--- | :--- |
| **Main Host Processor** | Raspberry Pi 4 Model B (Quad-Core Cortex-A72 @ 2.1 GHz) |
| **Embedded Controller** | ESP32 NodeMCU DevKit (Dual-Core Xtensa @ 240 MHz, FreeRTOS) |
| **Shared Memory** | `core/blackboard.py` (Mutex lock-free reads, zero-CPU `wait_for`) |
| **Vision Neural Model** | OpenCV YuNet ONNX (`face_detection_yunet_2023mar.onnx`) |
| **Vector DB & Model** | ChromaDB + `all-MiniLM-L6-v2` (384-dimensional dense vectors) |
| **Local Response Latency** | $< 40\text{ ms}$ (Local 48kHz WAV Cache Hit) |
| **Mic Node & Pi Zero 2 W** | Solved pin conflict between dual SPI eyes and I2S microphone |
| **Actuator Drivers** | PCA9685 12-bit PWM (Servos) + TB6612FNG (N20 Base Spin Motor) |
| **Proximity Sensors** | 3x ST VL53L0X Laser ToF sensors via TCA9548A I2C Multiplexer (`0x70`) |
