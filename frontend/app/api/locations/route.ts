import { NextResponse } from 'next/server';
import fs from 'fs/promises';
import path from 'path';

export async function GET() {
  try {
    const rootDir = process.env.BACKEND_DIR || path.join(process.cwd(), '..');
    const dataDir = path.join(rootDir, 'data');
    
    // Read all map graph files
    const files = await fs.readdir(dataDir);
    const mapFiles = files.filter(f => f.startsWith('map_graph_floor_') && f.endsWith('.json'));
    
    const allLocations: any[] = [];
    
    for (const file of mapFiles) {
      const floorStr = file.replace('map_graph_', '').replace('.json', '');
      const filePath = path.join(dataDir, file);
      
      try {
        const content = await fs.readFile(filePath, 'utf-8');
        const data = JSON.parse(content);
        
        if (data.nodes) {
          // Extract rooms (excluding waypoints)
          for (const node of data.nodes) {
            if (node.type !== 'waypoint' && node.label) {
              allLocations.push({
                id: node.id,
                label: node.label,
                floor: floorStr
              });
            }
          }
        }
      } catch (err) {
        console.error(`Failed to parse ${file}`, err);
      }
    }
    
    // Sort locations alphabetically by label
    allLocations.sort((a, b) => a.label.localeCompare(b.label));
    
    return NextResponse.json({ locations: allLocations });
  } catch (error) {
    console.error('Error fetching locations:', error);
    return NextResponse.json({ error: 'Failed to fetch locations', locations: [] }, { status: 500 });
  }
}
