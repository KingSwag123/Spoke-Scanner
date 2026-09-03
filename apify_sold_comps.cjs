const { ReplitConnectors } = require("@replit/connectors-sdk");

async function main() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  const connectors = new ReplitConnectors();
  const path =
    "/v2/actors/caffein.dev~ebay-sold-listings/" +
    "run-sync-get-dataset-items?format=json&clean=true&maxItems=12&timeout=60";
  const response = await connectors.proxy("apify", path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      keywords: [input.query],
      ebaySite: "ebay.com",
      daysToScrape: 30,
      count: 12,
      sortOrder: "endedRecently",
      itemLocation: "domestic",
    }),
  });
  if (!response.ok) {
    const detail = (await response.text()).slice(0, 300);
    process.stdout.write(
      JSON.stringify({ ok: false, error: `HTTP ${response.status}: ${detail}` }),
    );
    return;
  }
  process.stdout.write(JSON.stringify({ ok: true, items: await response.json() }));
}

main().catch((error) => {
  process.stdout.write(
    JSON.stringify({ ok: false, error: error?.name || "ConnectorError" }),
  );
});