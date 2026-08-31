import { defineConfig } from "@playwright/test";

// Two journeys, no more. Both run against the built app and the packets API
// serving committed files — no database, no eval run in flight.
export default defineConfig({
  testDir: "tests/e2e",
  timeout: 15_000,
  fullyParallel: false,
  reporter: "line",
  use: {
    baseURL: "http://127.0.0.1:5174",
    trace: "off",
    // The full chromium build, not the separate headless-shell binary. One
    // browser download rather than two, and `npx playwright install chromium`
    // is then genuinely all a fresh clone needs.
    channel: "chromium",
  },
  webServer: [
    {
      command: "cd .. && .venv/bin/python -m statesync.api.server",
      url: "http://127.0.0.1:8787/api/divergences",
      reuseExistingServer: true,
      timeout: 20_000,
    },
    {
      command: "npm run preview",
      url: "http://127.0.0.1:5174",
      reuseExistingServer: true,
      timeout: 30_000,
    },
  ],
});
