import 'package:flutter/material.dart';

class WelcomeView extends StatelessWidget {
  final VoidCallback onTapStart;

  const WelcomeView({required this.onTapStart, super.key});

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: const BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [
            Color(0xFF0F172A), // Slate 900
            Color(0xFF020617), // Slate 950
          ],
        ),
      ),
      child: Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            // Welcome Title
            const Text(
              "NEma Campus Kiosk",
              style: TextStyle(
                fontSize: 36,
                fontWeight: FontWeight.bold,
                color: Colors.white,
                letterSpacing: -0.5,
              ),
            ),
            const SizedBox(height: 12),
            const Text(
              "Your AI-Powered Voice Guide",
              style: TextStyle(
                fontSize: 18,
                color: Colors.blueGrey,
              ),
            ),
            const SizedBox(height: 48),

            // Tap to Talk pulse button
            GestureDetector(
              onTap: onTapStart,
              child: Container(
                width: 140,
                height: 140,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: Colors.blue.withOpacity(0.12),
                  border: Border.all(
                    color: Colors.blue.withOpacity(0.4),
                    width: 2,
                  ),
                ),
                child: Center(
                  child: Container(
                    width: 100,
                    height: 100,
                    decoration: const BoxDecoration(
                      shape: BoxShape.circle,
                      color: Colors.blue,
                    ),
                    child: const Icon(
                      Icons.mic,
                      size: 44,
                      color: Colors.white,
                    ),
                  ),
                ),
              ),
            ),
            const SizedBox(height: 24),
            const Text(
              "Tap the mic to start asking questions",
              style: TextStyle(
                fontSize: 15,
                color: Colors.blueGrey,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
