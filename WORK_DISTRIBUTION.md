# Voice Agent Kiosk - Project Work Distribution

This document outlines the comprehensive work distribution for the Voice Agent Kiosk project. The system is a complex integration of mechanical design, embedded electronics, AI-driven computer vision, natural language processing, and interactive web interfaces.

The workload has been distributed among the 5 team members based on their specific hardware and software assignments.

---                                                                                                                                          

## 1. Major Subsystems Identified 

To ensure complete end-to-end project delivery, the system is broken down into the following major subsystems:

- **Hardware:** Microcontrollers (Pi, Pi Zero), Displays (TFT), Cameras, Audio peripherals.
- **Embedded Systems:** Firmware, OS configuration, and low-level communication protocols.
- **PCB Design:** Custom circuit board layout and schematic design.
- **Mechanical Design:** 3D modeling, chassis design, and structural integrity.
- **Power System:** Battery management, voltage regulation, and power distribution.
- **Sensors:** Time-of-Flight (ToF), Proximity (Proxy meter), and IMU.
- **Motor Control:** Closed-loop control using encoder motors.
- **Raspberry Pi Software:** High-level orchestration, process management, and node communication.
- **AI Modules:** Inference engines for vision, gesture, and NLP.
- **Voice Processing:** Wake-word detection, speech-to-text, and audio filtering.
- **Computer Vision:** Real-time camera feeds, face tracking, and gesture recognition.
- **Navigation:** Spatial mapping, IMU tracking, and pathfinding.
- **User Interface:** Interactive 3D map kiosks, web dashboards, and robotic facial expressions.
- **Backend:** Cloud API integrations (Deepgram), server routing, and state management.
- **Testing:** Unit, integration, and user-acceptance testing.
- **System Integration:** Physical assembly and software pipeline merging.
- **Documentation:** Architectural diagrams, API specs, and project reports.

---

## 2. Team Member Assignments

### 👤 Makila
**Role: Lead UI/UX, Vision & Mechanical Designer**
Makila bridges the physical and digital interface of the robot, ensuring it looks approachable and tracks users intelligently.

**Assigned Tasks & Subsystems:**
*   **3D Design (Mechanical Design):** Designing the physical chassis, camera mounts, and display housings.
*   **Face Tracking Pi Camera (Computer Vision / AI / Hardware):** Implementing the vision pipeline to detect and continuously track human faces using the primary Pi Camera.
*   **Emotion with TFT Displays (User Interface / Embedded Systems):** Programming the TFT screens to render dynamic, responsive robotic facial expressions (eyes/emotions) based on system state.
*   **Web Design (User Interface / Backend):** Designing and developing the interactive web kiosk (frontend and backend endpoints) for user interaction.
*   **Event Poster & Knowledge Base Management (Web UI):** Developed the React-based admin upload portal for dynamically injecting event posters and custom Q&A factual knowledge into the robot's brain.
*   **Proactive Face Greeting:** Implemented the background polling and logic for the robot to automatically initiate greetings when a user's face is detected.

**Components & Technologies Used:**
*   **Hardware/Sensors:** Raspberry Pi Camera Module, TFT SPI/I2C Displays, 3D Printed Chassis (PLA/PETG).
*   **Software/Tech:** OpenCV, Mediapipe/Yunet (Face Tracking), React/Next.js & TailwindCSS (Web UI), Python (Pygame/Pillow for TFT), SolidWorks/Fusion360 (3D Modeling).

---

### 👤 Ruwan
**Role: Motion & Gesture Intelligence Lead**
Ruwan focuses on the spatial awareness of the robot and how it interprets physical human interactions.

**Assigned Tasks & Subsystems:**
*   **PCB Design (PCB Design):** Designing the custom printed circuit boards required to interface the Raspberry Pi with the various sensors and motors securely.
*   **Hand Control "Hi, Bye" & Hand Gestures (Computer Vision / AI Modules):** Training and implementing the machine learning models (`ByeWaveService`, `ArmSafetyEnvelope`) required to detect and interpret specific human hand gestures safely.
*   **IMU Implementation (Sensors / Navigation):** Integrating the Inertial Measurement Unit to track the robot's physical orientation, tilt, and acceleration for smooth movement and balance.

**Components & Technologies Used:**
*   **Hardware/Sensors:** Custom PCB (FR4), MPU6050/BNO085 (IMU sensor), Raspberry Pi Camera Module.
*   **Software/Tech:** Altium/KiCad/EasyEDA (PCB design), OpenCV, Mediapipe (Hand Landmarks), Python, I2C/SPI protocols.

---

### 👤 Vihanga
**Role: Navigation & Electronics Hardware Lead**
Vihanga is responsible for the robot's internal nervous system (custom circuitry) and its understanding of the environment.

**Assigned Tasks & Subsystems:**
*   **Map Design (Navigation / User Interface):** Architecting the internal spatial map (2D/3D graphs) and the logic for the robot to understand where it is and where it can go.
*   **Isometric 3D Map (React Three Fiber):** Built the interactive `NavigationMap` UI allowing users to visually explore floors and rooms on the kiosk screen.
*   **ToF Sensor Setup (Sensors / Embedded Systems):** Integrating Time-of-Flight sensors for accurate distance measurement and obstacle avoidance.
*   **Proxy Meter Setup (Sensors):** Configuring proximity sensors for close-range environmental awareness and safety.

**Components & Technologies Used:**
*   **Hardware/Sensors:** VL53L0X / VL53L1X (Time-of-Flight sensors), Ultrasonic/Infrared Proximity sensors (Proxy meter), I2C Multiplexers (TCA9548A).
*   **Software/Tech:** Python, ROS (Robot Operating System) or custom graph-based pathfinding (A* / Dijkstra), React Three Fiber (for 3D UI Map).

---

### 👤 Devinda
**Role: Actuation & Systems Integration Lead**
Devinda turns the software commands into physical movement and ensures the hardware is physically constructed correctly.

**Assigned Tasks & Subsystems:**
*   **Encoder Motor Use (Motor Control / Embedded Systems):** Writing the closed-loop PID control software to drive the motors precisely using encoder feedback, ensuring accurate locomotion.
*   **Conversational Arm Gestures (`TalkGestureService`):** Implemented dynamic robotic arm kinematics that synchronize physically with the robot's speaking state.
*   **Assembling PCB & Hardware (Hardware / System Integration):** Physically soldering the PCBs, wiring the harnesses, integrating the power systems, and assembling the final physical robot.

**Components & Technologies Used:**
*   **Hardware/Sensors:** DC Gear Motors with Encoders, Motor Drivers (L298N/TB6612FNG), Batteries (LiPo/18650), Jumper wires, Soldering equipment.
*   **Software/Tech:** PID Control algorithms, PWM (Pulse Width Modulation), Python/C++ (for precise timing).

---

### 👤 Methoo
**Role: Voice, NLP & Edge Audio Lead**
Methoo handles the entire conversational pipeline, giving the robot its ability to hear, understand, and respond.

**Assigned Tasks & Subsystems:**
*   **Voice Detection (Voice Processing):** Implementing local wake-word detection and silence-trimming logic.
*   **Deepgram & Natural Language Processing (AI Modules / Backend):** Integrating the Deepgram API for blazing-fast speech-to-text, and handling the NLP pipeline to determine user intent.
*   **Offline NLU & Intent Compiler (`NluServer`):** Built the `SentenceTransformer` / Numpy vector database pipeline to securely process offline user intents and serve instant pre-synthesized audio.
*   **Audio Sync & Echo Suppression:** Implemented the `SpeechSyncService` and rolling-window echo cancellation to ensure the robot doesn't hear itself over the speakers.
*   **USB Microphone with Pi Zero (Hardware / Embedded Systems):** Setting up a satellite Pi Zero dedicated entirely to high-fidelity audio capture via a USB microphone array, ensuring the main Pi is not bottlenecked.

**Components & Technologies Used:**
*   **Hardware/Sensors:** Raspberry Pi Zero (W/2W), USB Microphone Array (e.g., ReSpeaker), Speakers.
*   **Software/Tech:** Deepgram API (Speech-to-Text), Wake-word engines (Porcupine/Snowboy), NLP libraries (NLTK/spaCy/LLM), WebSockets/REST APIs.

---

## 3. Shared Responsibilities

While the tasks above are highly specialized, the following subsystems require collaborative effort across all members:

*   **Power System:** Devinda (Assembly) and Vihanga (PCB Design) must collaborate to ensure the batteries and voltage regulators can handle the load of the Pi, Pi Zero, Motors, and TFTs.
*   **Raspberry Pi Software:** All members must ensure their individual Python scripts/modules communicate efficiently over the shared system architecture (e.g., WebSockets, ROS, or local sockets).
*   **Testing & System Integration:** Combining Vihanga's map, Devinda's motors, Ruwan's IMU, Makila's face tracking, and Methoo's voice commands into a single, cohesive, bug-free system.
*   **Documentation:** Every member is responsible for documenting their specific subsystems, APIs, wiring diagrams, and setup instructions.
