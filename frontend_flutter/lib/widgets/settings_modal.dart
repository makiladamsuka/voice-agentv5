import 'package:flutter/material.dart';

class SettingsModal extends StatefulWidget {
  final String currentServerUrl;
  final Function(String newUrl) onSave;

  const SettingsModal({
    required this.currentServerUrl,
    required this.onSave,
    super.key,
  });

  @override
  State<SettingsModal> createState() => _SettingsModalState();
}

class _SettingsModalState extends State<SettingsModal> {
  late TextEditingController _urlController;

  @override
  void initState() {
    super.override
    _urlController = TextEditingController(text: widget.currentServerUrl);
  }

  @override
  void dispose() {
    _urlController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Row(
        children: [
          Icon(Icons.settings, color: Colors.blueAccent),
          SizedBox(width: 10),
          Text("Kiosk Settings"),
        ],
      ),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Text(
              "NLU WebSocket Server URL",
              style: TextStyle(fontSize: 14, color: Colors.white60),
            ),
            const SizedBox(height: 8),
            TextField(
              controller: _urlController,
              decoration: const InputDecoration(
                border: OutlineInputBorder(),
                hintText: "ws://localhost:8765/ws/voice",
              ),
              style: const TextStyle(color: Colors.white),
            ),
            const SizedBox(height: 16),
            const Text(
              "Audio Output: HDMI / USB Auto-discovered",
              style: TextStyle(fontSize: 13, color: Colors.greenAccent),
            ),
            const SizedBox(height: 8),
            const Text(
              "Graphics Driver: DRM/KMS Framebuffer (vc4-kms-v3d)",
              style: TextStyle(fontSize: 13, color: Colors.white38),
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text("Cancel", style: TextStyle(color: Colors.white54)),
        ),
        ElevatedButton(
          onPressed: () {
            widget.onSave(_urlController.text);
            Navigator.of(context).pop();
          },
          style: ElevatedButton.styleFrom(backgroundColor: Colors.blueAccent),
          child: const Text("Save Changes", style: TextStyle(color: Colors.white)),
        ),
      ],
    );
  }
}
