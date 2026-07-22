import { NextResponse } from "next/server";
import fs from "fs/promises";
import path from "path";

const CACHE_FILE = path.join(process.cwd(), ".facebook-cache.json");
const CACHE_DURATION = 10 * 60 * 1000; // 10 minutes

// Fallback data is now ONLY shown the very first time the app is ever booted before the first scrape finishes
const fallbackData = [
  {
    id: "fitmoments_1",
    full_picture:
      "https://images.unsplash.com/photo-1571019614242-c5c5dee9f50b?ixlib=rb-4.0.3&auto=format&fit=crop&w=1000&q=80",
    message:
      "Start your morning right! Join our sunrise yoga sessions every Tuesday at the main campus quad. Don't forget your mat! 🧘‍♀️✨ #FitMoments #CampusWellness",
    created_time: new Date().toISOString(),
  },
];

export async function GET() {
  let cachedPosts = null;
  let lastFetchTime = 0;

  // Attempt to read the persisted cache from disk
  try {
    const fileContent = await fs.readFile(CACHE_FILE, "utf-8");
    const parsed = JSON.parse(fileContent);
    cachedPosts = parsed.posts;
    lastFetchTime = parsed.timestamp;
  } catch (err) {
    // Cache file doesn't exist yet
  }

  const needsRefresh =
    !cachedPosts || Date.now() - lastFetchTime > CACHE_DURATION;

  if (needsRefresh) {
    // Fetch fresh data synchronously so the response is immediately up to date
    const success = await triggerRapidApiScrape();
    if (!success) {
      // On failure, update the timestamp with the old data to prevent spamming the API
      await fs.writeFile(
        CACHE_FILE,
        JSON.stringify(
          {
            timestamp: Date.now(),
            posts: cachedPosts || fallbackData,
          },
          null,
          2
        )
      ).catch(() => {});
    }
    try {
      const fileContent = await fs.readFile(CACHE_FILE, "utf-8");
      const parsed = JSON.parse(fileContent);
      cachedPosts = parsed.posts;
    } catch (err) {
      // Ignore read errors
    }
  }

  // Return the persisted real data instantly (or the fallback if this is the first boot ever)
  return NextResponse.json(cachedPosts || fallbackData);
}

async function triggerRapidApiScrape() {
  try {
    const RAPIDAPI_KEY = process.env.RAPIDAPI_KEY?.trim();
    const FB_PAGE_ID = process.env.FACEBOOK_PAGE_ID?.trim() || "fitmoments";

    if (!RAPIDAPI_KEY) {
      console.error("RAPIDAPI_KEY is not set in environment variables.");
      return false;
    }

    console.log(`Starting background RapidAPI scrape for ${FB_PAGE_ID}...`);

    const response = await fetch(
      `https://facebook-pages-scraper2.p.rapidapi.com/get_facebook_posts_details?link=https%3A%2F%2Fwww.facebook.com%2F${FB_PAGE_ID}&timezone=UTC`,
      {
        method: "GET",
        headers: {
          "x-rapidapi-key": RAPIDAPI_KEY,
          "x-rapidapi-host": "facebook-pages-scraper2.p.rapidapi.com",
        },
      },
    );

    if (!response.ok) {
      console.error("RapidAPI failed:", await response.text());
      return false;
    }

    const json = await response.json();
    const items = json.data?.posts || [];

    const formattedPosts = items.slice(0, 5).map((item: any, index: number) => {
      // Extract Image and Text from ousema.frikha's RapidAPI response format
      let imgUrl = null;
      if (item.attachments?.all_subattachments?.nodes?.length > 0) {
        imgUrl =
          item.attachments.all_subattachments.nodes[0]?.media?.image?.uri;
      }
      const message =
        item.values?.text ||
        item.text ||
        item.basic_info?.title ||
        item.basic_info?.text ||
        "New update from FIT Moments!";

      return {
        id: `rapidapi_${item.basic_info?.post_id || index}_${Date.now()}`,
        full_picture:
          imgUrl ||
          "https://images.unsplash.com/photo-1571019614242-c5c5dee9f50b?ixlib=rb-4.0.3",
        message:
          message.length > 150 ? message.substring(0, 150) + "..." : message,
        created_time: item.basic_info?.created_time
          ? new Date(item.basic_info.created_time).toISOString()
          : new Date().toISOString(),
      };
    });

    if (formattedPosts.length > 0) {
      // Persist the real posts to disk so they survive server restarts!
      await fs.writeFile(
        CACHE_FILE,
        JSON.stringify(
          {
            timestamp: Date.now(),
            posts: formattedPosts,
          },
          null,
          2,
        ),
      );
      console.log(
        "Successfully cached new Facebook posts from RapidAPI to disk!",
      );
      return true;
    }
    return false;
  } catch (err) {
    console.error("Background scrape error:", err);
    return false;
  }
}
