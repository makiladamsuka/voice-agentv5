require("dotenv").config({ path: ".env.local" });
const fs = require("fs");
async function test() {
  const RAPIDAPI_KEY = process.env.RAPIDAPI_KEY;
  if (!RAPIDAPI_KEY) {
    console.log("No key");
    return;
  }
  const FB_PAGE_ID = "fitmoments";
  const res = await fetch(
    `https://facebook-pages-scraper2.p.rapidapi.com/get_facebook_posts_details?link=https%3A%2F%2Fwww.facebook.com%2F${FB_PAGE_ID}&timezone=UTC`,
    {
      headers: {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": "facebook-pages-scraper2.p.rapidapi.com",
      },
    },
  );
  const json = await res.json();
  fs.writeFileSync(
    "rapidapi_sample.json",
    JSON.stringify(json.data?.posts?.[0] || {}, null, 2),
  );
  console.log("Written to rapidapi_sample.json");
}
test();
