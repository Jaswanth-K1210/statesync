// Paise is the stored unit; rupees is the displayed one. A human never sees
// raw paise, and the conversion lives in exactly one place so it cannot drift.

export function rupees(paise: number): string {
  const sign = paise < 0 ? "-" : "";
  const abs = Math.abs(paise);
  const whole = Math.floor(abs / 100).toLocaleString("en-IN");
  const fraction = String(abs % 100).padStart(2, "0");
  return `${sign}₹${whole}.${fraction}`;
}
