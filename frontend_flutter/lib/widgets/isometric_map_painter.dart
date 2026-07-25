import 'dart:math';
import 'package:flutter/material.dart';

class IsometricMapPainter extends CustomPainter {
  final List<dynamic> pathCoords;
  final String? destination;
  final double animationPercent;

  IsometricMapPainter({
    required this.pathCoords,
    required this.destination,
    required this.animationPercent,
  });

  // Convert 3D space grid coordinates (x, y, z) into 2D isometric coordinates
  Offset _toIsometric(double x, double y, double z, Size size) {
    // 30 degree isometric angle projection
    const double angle = 30 * pi / 180;
    final double cosAngle = cos(angle);
    final double sinAngle = sin(angle);

    // Grid scaling factors
    const double scale = 24.0;
    
    // Center point of the canvas
    final double centerX = size.width / 2;
    final double centerY = size.height * 0.65;

    double isoX = centerX + (x - y) * scale * cosAngle;
    double isoY = centerY + (x + y) * scale * sinAngle - (z * scale * 1.5);

    return Offset(isoX, isoY);
  }

  @override
  void paint(Canvas canvas, Size size) {
    // 1. Draw floor grids (e.g. 10x10 grids for level 0 and level 1)
    final gridPaint = Paint()
      ..color = Colors.blue.withOpacity(0.08)
      ..strokeWidth = 1.0
      ..style = PaintingStyle.stroke;

    for (int floor = 0; floor <= 1; floor++) {
      final double z = floor.toDouble();
      for (int i = 0; i <= 10; i++) {
        // Draw grid lines along X axis
        final startX = _toIsometric(0.0, i.toDouble(), z, size);
        final endX = _toIsometric(10.0, i.toDouble(), z, size);
        canvas.drawLine(startX, endX, gridPaint);

        // Draw grid lines along Y axis
        final startY = _toIsometric(i.toDouble(), 0.0, z, size);
        final endY = _toIsometric(i.toDouble(), 10.0, z, size);
        canvas.drawLine(startY, endY, gridPaint);
      }
    }

    // 2. Draw mock building blocks (Auditoriums, Labs, etc.)
    final blockPaint = Paint()
      ..color = Colors.indigo.withOpacity(0.15)
      ..style = PaintingStyle.fill;
    final blockBorderPaint = Paint()
      ..color = Colors.indigo.withOpacity(0.3)
      ..strokeWidth = 1.5
      ..style = PaintingStyle.stroke;

    // We draw mock rooms at predefined coordinates
    final mockRooms = [
      {'name': 'Lecture Hall 1', 'x': 2.0, 'y': 3.0, 'z': 0.0, 'w': 2.0, 'h': 2.0},
      {'name': 'Lecture Hall 2', 'x': 5.0, 'y': 3.0, 'z': 0.0, 'w': 2.0, 'h': 2.0},
      {'name': 'Auditorium 1', 'x': 2.0, 'y': 6.0, 'z': 0.0, 'w': 3.0, 'h': 3.0},
      {'name': 'Main Office', 'x': 7.0, 'y': 6.0, 'z': 1.0, 'w': 2.0, 'h': 2.0},
    ];

    for (var room in mockRooms) {
      final rx = room['x'] as double;
      final ry = room['y'] as double;
      final rz = room['z'] as double;
      final rw = room['w'] as double;
      final rh = room['h'] as double;

      final path = Path();
      path.moveTo(_toIsometric(rx, ry, rz, size).dx, _toIsometric(rx, ry, rz, size).dy);
      path.lineTo(_toIsometric(rx + rw, ry, rz, size).dx, _toIsometric(rx + rw, ry, rz, size).dy);
      path.lineTo(_toIsometric(rx + rw, ry + rh, rz, size).dx, _toIsometric(rx + rw, ry + rh, rz, size).dy);
      path.lineTo(_toIsometric(rx, ry + rh, rz, size).dx, _toIsometric(rx, ry + rh, rz, size).dy);
      path.close();

      canvas.drawPath(path, blockPaint);
      canvas.drawPath(path, blockBorderPaint);
    }

    // 3. Draw calculated path connecting coordinate list
    if (pathCoords.isNotEmpty) {
      final pathPaint = Paint()
        ..color = Colors.orangeAccent
        ..strokeWidth = 4.0
        ..strokeCap = StrokeCap.round
        ..strokeJoin = StrokeJoin.round
        ..style = PaintingStyle.stroke;

      final animatedPath = Path();
      final startPt = pathCoords[0];
      final startOffset = _toIsometric(
        (startPt[0] as num).toDouble(),
        (startPt[1] as num).toDouble(),
        (startPt[2] as num).toDouble(),
        size,
      );
      animatedPath.moveTo(startOffset.dx, startOffset.dy);

      // Determine how many path nodes to draw based on animation percentage
      final int nodesToDraw = (pathCoords.length * animationPercent).ceil().clamp(1, pathCoords.length);

      for (int i = 1; i < nodesToDraw; i++) {
        final node = pathCoords[i];
        final offset = _toIsometric(
          (node[0] as num).toDouble(),
          (node[1] as num).toDouble(),
          (node[2] as num).toDouble(),
          size,
        );
        animatedPath.lineTo(offset.dx, offset.dy);
      }

      canvas.drawPath(animatedPath, pathPaint);

      // Draw start pin
      final startPinPaint = Paint()..color = Colors.green;
      canvas.drawCircle(startOffset, 6.0, startPinPaint);

      // Draw end pin
      if (nodesToDraw == pathCoords.length) {
        final endNode = pathCoords.last;
        final endOffset = _toIsometric(
          (endNode[0] as num).toDouble(),
          (endNode[1] as num).toDouble(),
          (endNode[2] as num).toDouble(),
          size,
        );
        final endPinPaint = Paint()..color = Colors.red;
        canvas.drawCircle(endOffset, 6.0, endPinPaint);
      }
    }
  }

  @override
  bool shouldRepaint(covariant IsometricMapPainter oldDelegate) {
    return oldDelegate.pathCoords != pathCoords ||
        oldDelegate.animationPercent != animationPercent ||
        oldDelegate.destination != destination;
  }
}
