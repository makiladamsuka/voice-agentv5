# Voice Agent V5 — 5-Minute Presentation Script (Simple English)

**Title:** Voice Agent V5: Interactive Campus Kiosk Robot  
**Target Duration:** 5 Minutes (~650 spoken words @ 130 wpm)  
**Tone:** Plain, clear, simple conversational English (no complex academic jargon)  
**File Location:** `docs/INTRODUCTORY_SPEECH_5MIN.md`  

---

## ⏱️ Speech Schedule & Timing

```
[0:00 - 1:00]  PART 1: Introduction & What the Robot Does
[1:00 - 2:15]  PART 2: How the Brain & Motors Work Together
[2:15 - 3:30]  PART 3: Camera Vision & Instant Voice AI
[3:30 - 4:15]  PART 4: Clever Solutions to Hardware Problems
[4:15 - 5:00]  PART 5: Results & Conclusion
```

---

## 🎙️ Spoken Speech Script (Verbatim)

### PART 1: Introduction & What the Robot Does (0:00 – 1:00)

> "Hello everyone! Today, I’m excited to show you **Voice Agent V5**, an interactive kiosk robot we built for our campus.
> 
> Imagine walking into a hall or building on campus. Instead of using a boring, quiet touchscreen, you meet a friendly robot. When you walk up to it, the robot turns its head to face you, waves its arm, and talks to you! You can ask questions like *'Where is the hackathon?'* or *'What events are happening today?'*, and it answers you instantly in a natural voice.
> 
> Most public kiosks are just flat screens that no one wants to talk to. And most fancy humanoid robots cost thousands of dollars and take 3 seconds to respond over the internet. We built Voice Agent V5 to solve this—creating a fast, friendly, interactive robot using low-cost, smart hardware."

---

### PART 2: How the Brain & Motors Work Together (1:00 – 2:15)

> "So, how does everything work under the hood?
> 
> We use a **Raspberry Pi 4** as the main brain. It runs the camera vision, handles the voice AI, and powers the touchscreen display you see on the kiosk.
> 
> To control the physical robot body, we use a small, separate microcontroller called the **ESP32**. The ESP32 acts like the muscles—it controls the motors that turn the head, wave the arm, and spin the robot base around when someone approaches.
> 
> The Raspberry Pi talks to the ESP32 using a USB serial connection. To keep everything fast and organized, we built a shared memory system called the **Blackboard**. All parts of the robot—the camera, sensors, motors, and voice—share information through this Blackboard cleanly without slowing down the computer."

---

### PART 3: Camera Vision & Instant Voice AI (2:15 – 3:30)

> "Now, let’s talk about the AI.
> 
> First, for vision: We use a camera with a face-detection AI model called **YuNet**. The camera tracks your face so the robot's head looks right at you. When you start talking, the robot automatically pauses camera tracking for a moment so the computer can focus 100% on audio.
> 
> Second, for voice: Usually, AI voice agents take 2 to 3 seconds to answer because they send audio to servers on the internet. That delay feels slow and awkward.
> 
> To fix this, we created an instant local search system. When we add event posters, our system automatically creates questions and saves pre-made audio files on the Pi. When a student asks a question, the robot matches the question using vector search (`all-MiniLM-L6-v2`) and plays the answer **instantly—in under 40 milliseconds**! If someone asks something new, it automatically falls back to online AI models like Groq Llama 3."

---

### PART 4: Clever Solutions to Hardware Problems (3:30 – 4:15)

> "While building this robot, we ran into some tough hardware problems, but we found clever solutions.
> 
> For example: When we ran the robot's animated screen eyes on the Pi 4, the screen wiring clashed with the digital microphone wiring. If we plugged both into the Pi 4, the microphone stopped working!
> 
> To fix this, we added a small, cheap **Raspberry Pi Zero 2 W** dedicated just to the microphone. This separated the mic from the main board and stopped motor electrical noise from messing up the audio.
> 
> We also added three laser distance sensors (Time-of-Flight sensors) on the left, center, and right. These act like eyes that tell the robot when someone is walking up to it from up to 2 meters away!"

---

### PART 5: Results & Conclusion (4:15 – 5:00)

> "Here are our main results:
> - The camera tracks faces smoothly at **20 frames per second**.
> - The head and arm motors move smoothly at **50 updates per second**.
> - Voice answers start playing in **less than 40 milliseconds**.
> - The computer stays cool and doesn’t overheat.
> 
> To wrap up: Voice Agent V5 proves that you don't need super expensive hardware to build a fast, smart, interactive robot. By combining smart software, low-cost microcontrollers, and fast local vector search, we created a fun and useful robot for our campus.
> 
> Thank you so much! I’d be happy to answer any questions."

---

## 📌 Quick Summary Card (Cheat Sheet)

| Topic | Simple Explanation |
| :--- | :--- |
| **Main Brain** | Raspberry Pi 4 Model B (Runs AI, Vision, Voice, and Touchscreen UI) |
| **Motor Muscle** | ESP32 Microcontroller (Controls head servos, arm servos, and base spin motor) |
| **Shared Memory** | `Blackboard` (`core/blackboard.py`) — connects vision, motors, and voice smoothly |
| **Face Tracking** | YuNet AI model (tracks user's face so head follows you) |
| **Instant Voice Search** | Local vector search (`all-MiniLM-L6-v2`) plays pre-saved audio in $<40\text{ ms}$ |
| **Microphone Fix** | Dedicated Pi Zero 2 W node (solves pin conflict with screen eyes & motor noise) |
| **Distance Sensors** | 3x Laser Distance (ToF) sensors detect when someone walks up to the robot |
