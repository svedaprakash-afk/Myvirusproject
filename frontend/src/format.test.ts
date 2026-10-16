import { describe, expect, it } from "vitest";
import { compact, money, rateLabel, toMinor } from "./format";

describe("format", () => {
  it("uses Indian digit grouping", () => {
    expect(money(12345678)).toBe("₹1,23,456.78");
    expect(money(-500)).toBe("−₹5.00");
    expect(money(null)).toBe("—");
  });
  it("compacts to K / L / Cr", () => {
    expect(compact(12_00_00_000_00)).toBe("₹12.00 Cr");
    expect(compact(34_50_000_00)).toBe("₹34.50 L");
    expect(compact(1_250_00)).toBe("₹1.3 K");
  });
  it("never renders a foreign currency with a rupee sign and groups by currency convention", () => {
    expect(money(1234567, "USD")).toBe("$12,345.67");
    expect(money(-3500, "USD")).toBe("−$35.00");
    expect(money(1234567)).toBe("₹12,345.67");
    expect(money(5000000, { ccy: "EUR", sign: true })).toBe("+€50,000.00");
    expect(compact(250_000_000, "USD")).toBe("$2.50 M");
    expect(money(100, "CNY")).toBe("CN¥1.00");
    expect(money(100, "HKD")).toBe("HK$1.00");
    expect(money(12345, "JPY")).toBe("¥123.45");
  });
  it("parses rupee input to exact paise without float error", () => {
    expect(toMinor("1,18,000.10")).toBe(11800010);
    expect(toMinor("0.29")).toBe(29);
    expect(toMinor("19.99")).toBe(1999);
    expect(Number.isNaN(toMinor("12.345"))).toBe(true);
    expect(Number.isNaN(toMinor("abc"))).toBe(true);
  });
  it("formats TDS rates", () => {
    expect(rateLabel(200)).toBe("2%");
    expect(rateLabel(10)).toBe("0.1%");
    expect(rateLabel(null)).toBe("—");
  });
});
