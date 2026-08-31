import { expect, test } from "@playwright/test";

// Two journeys, written as what an ops person does — not as clicks.

test("ops reviews a refusal and sees why every candidate failed", async ({ page }) => {
  await page.goto("/");

  await page.getByRole("button", { name: "no hypothesis verified" }).click();
  await page.getByRole("button", { name: /pay_hc14/ }).click();

  // The residual is the question everything below is answering.
  await expect(page.getByTestId("residual")).toHaveText("₹11.40");
  await expect(page.getByTestId("reason-code")).toHaveText("no_hypothesis_verified");

  // Every candidate is present, with its arithmetic and its failure reason.
  const cards = page.getByTestId("hypothesis");
  await expect(cards).toHaveCount(5);
  await expect(cards.first()).toContainText("=");
  await expect(page.getByText(/off by/).first()).toBeVisible();

  // Rejected ones are shown, not hidden behind a toggle.
  await expect(page.locator(".badge.ARITHMETIC_FAILED").first()).toBeVisible();
});

test("ops distinguishes an ambiguous case at a glance", async ({ page }) => {
  await page.goto("/");

  await page.getByRole("button", { name: "ambiguous multiple verified" }).click();
  await page.getByRole("button", { name: /pay_hc13/ }).click();

  await expect(page.getByTestId("reason-code")).toHaveText("ambiguous_multiple_verified");

  // Exactly two verified, and visibly different breakdowns — that is the whole
  // point of the screen: the system found two explanations and refused to pick.
  const verified = page.locator(".badge.VERIFIED");
  await expect(verified).toHaveCount(2);

  const cards = page.locator(".hyp.verified");
  const first = await cards.nth(0).innerText();
  const second = await cards.nth(1).innerText();
  expect(first).not.toEqual(second);
});
