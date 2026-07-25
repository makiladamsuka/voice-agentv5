import 'package:flutter/material.dart';
import '../services/nlu_websocket_client.dart';
import '../widgets/isometric_map_painter.dart';

class SessionView extends StatefulWidget {
  final NluVoiceState voiceState;
  final double micVolume;
  final String lastTranscript;
  final List<Map<String, String>> chatHistory;
  final Map<String, dynamic>? lastAction;
  final VoidCallback onTapStop;

  const SessionView({
    required this.voiceState,
    required this.micVolume,
    required this.lastTranscript,
    required this.chatHistory,
    required this.lastAction,
    required this.onTapStop,
    super.key,
  });

  @override
  State<SessionView> createState() => _SessionViewState();
}

class _SessionViewState extends State<SessionView> with SingleTickerProviderStateMixin {
  late AnimationController _mapAnimationController;

  @override
  void initState() {
    super.initState();
    _mapAnimationController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 2500),
    );

    if (widget.lastAction != null && widget.lastAction!['action'] == 'navigate') {
      _mapAnimationController.forward(from: 0.0);
    }
  }

  @override
  void didUpdateWidget(covariant SessionView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.lastAction != oldWidget.lastAction &&
        widget.lastAction != null &&
        widget.lastAction!['action'] == 'navigate') {
      _mapAnimationController.forward(from: 0.0);
    }
  }

  @override
  void dispose() {
    _mapAnimationController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final hasActiveMap = widget.lastAction != null && widget.lastAction!['action'] == 'navigate';
    final pathCoords = hasActiveMap ? (widget.lastAction!['path_coords'] ?? []) as List<dynamic> : [];
    final destination = hasActiveMap ? widget.lastAction!['destination'] as String? : null;

    return Container(
      color: const Color(0xFF020617), // Deep Dark Slate
      child: Stack(
        children: [
          // 1. Map Canvas Overlay (if navigation action is active)
          if (hasActiveMap)
            Positioned.fill(
              child: AnimatedBuilder(
                animation: _mapAnimationController,
                builder: (context, child) {
                  return CustomPaint(
                    painter: IsometricMapPainter(
                      pathCoords: pathCoords,
                      destination: destination,
                      animationPercent: _mapAnimationController.value,
                    ),
                  );
                },
              ),
            ),

          // 2. Chat UI overlay
          Positioned.fill(
            child: Padding(
              padding: const EdgeInsets.all(24.0),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  // App Header
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: [
                      ElevatedButton.icon(
                        onPressed: widget.onTapStop,
                        icon: const Icon(Icons.arrow_back),
                        label: const Text("Exit Voice"),
                        style: ElevatedButton.styleFrom(
                          backgroundColor: Colors.white12,
                          foregroundColor: Colors.white,
                        ),
                      ),
                      Text(
                        widget.voiceState.name.toUpperCase(),
                        style: TextStyle(
                          color: _getStateColor(widget.voiceState),
                          fontWeight: FontWeight.bold,
                          letterSpacing: 1.2,
                        ),
                      ),
                    ],
                  ),
                  const Spacer(),

                  // Transcript panel at the bottom half
                  if (!hasActiveMap) _buildTranscriptPanel(),

                  const SizedBox(height: 24),

                  // Real-time voice pulse (Siri-like widget)
                  if (widget.voiceState == NluVoiceState.listening)
                    Center(
                      child: AnimatedContainer(
                        duration: const Duration(milliseconds: 50),
                        width: 80 + (widget.micVolume * 150),
                        height: 80 + (widget.micVolume * 150),
                        decoration: BoxDecoration(
                          shape: BoxShape.circle,
                          gradient: RadialGradient(
                            colors: [
                              Colors.blue.withOpacity(0.6),
                              Colors.blue.withOpacity(0.1),
                              Colors.transparent,
                            ],
                          ),
                        ),
                        child: const Icon(
                          Icons.mic,
                          color: Colors.white,
                          size: 32,
                        ),
                      ),
                    ),

                  const SizedBox(height: 16),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildTranscriptPanel() {
    return Container(
      height: 320,
      decoration: BoxDecoration(
        color: Colors.black45,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: Colors.white10),
      ),
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text(
            "Conversation History",
            style: TextStyle(color: Colors.white38, fontSize: 13, fontWeight: FontWeight.bold),
          ),
          const Divider(color: Colors.white10),
          Expanded(
            child: ListView.builder(
              itemCount: widget.chatHistory.length,
              itemBuilder: (context, index) {
                final chat = widget.chatHistory[index];
                final isUser = chat['sender'] == 'user';
                return Padding(
                  padding: const EdgeInsets.symmetric(vertical: 6.0),
                  child: Align(
                    alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
                    child: Container(
                      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
                      decoration: BoxDecoration(
                        color: isUser ? Colors.blue.withOpacity(0.2) : Colors.white10,
                        borderRadius: BorderRadius.circular(12),
                        border: Border.all(
                          color: isUser ? Colors.blue.withOpacity(0.4) : Colors.white12,
                        ),
                      ),
                      child: Text(
                        chat['text'] ?? "",
                        style: const TextStyle(color: Colors.white, fontSize: 15),
                      ),
                    ),
                  ),
                );
              },
            ),
          ),
          if (widget.lastTranscript.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 8.0),
              child: Text(
                "Listening: \"${widget.lastTranscript}\"",
                style: const TextStyle(color: Colors.blueAccent, fontStyle: FontStyle.italic),
              ),
            ),
        ],
      ),
    );
  }

  Color _getStateColor(NluVoiceState state) {
    switch (state) {
      case NluVoiceState.listening:
        return Colors.greenAccent;
      case NluVoiceState.thinking:
        return Colors.orangeAccent;
      case NluVoiceState.speaking:
        return Colors.blueAccent;
      default:
        return Colors.white30;
    }
  }
}
