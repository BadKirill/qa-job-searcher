import { expect, type Page } from "@playwright/test";

export async function expectReadableColors(page: Page) {
  const failures = await page.evaluate(() => {
    type Color = [number, number, number, number];
    const parse = (value: string): Color => {
      const srgbPrefix = "color(srgb ";
      const isNormalized = value.startsWith(srgbPrefix);
      const start = isNormalized ? srgbPrefix.length : value.indexOf("(") + 1;
      const [red, green, blue, alpha = 1] = value.slice(start, -1).split(/[,/\s]+/).map(Number);
      const scale = isNormalized ? 255 : 1;
      return [red * scale, green * scale, blue * scale, alpha];
    };
    const over = (front: Color, back: Color): Color => [
      front[0] * front[3] + back[0] * (1 - front[3]),
      front[1] * front[3] + back[1] * (1 - front[3]),
      front[2] * front[3] + back[2] * (1 - front[3]),
      1,
    ];
    const luminance = (color: Color) => {
      const linear = color.slice(0, 3).map((channel) => {
        const value = channel / 255;
        return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
      });
      return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
    };
    const background = (element: Element): Color => {
      const layers: Color[] = [];
      for (let parent: Element | null = element; parent; parent = parent.parentElement) {
        layers.unshift(parse(getComputedStyle(parent).backgroundColor));
      }
      return layers.reduce((back, front) => over(front, back), [255, 255, 255, 1] as Color);
    };
    const textFailures = Array.from(document.querySelectorAll<HTMLElement>("body *"))
      .filter((element) =>
        element.getClientRects().length > 0 &&
        !element.closest(":disabled, [aria-disabled=true], .sr-only") &&
        (Array.from(element.childNodes).some((child) => child.nodeType === Node.TEXT_NODE && child.textContent!.trim()) ||
          element.matches("input:not([type=checkbox]):not([type=file]), textarea, select")),
      )
      .flatMap((element) => {
        const style = getComputedStyle(element);
        if (style.visibility !== "visible" || style.opacity === "0") return [];
        const back = background(element);
        const front = over(parse(style.color), back);
        const values = [luminance(front), luminance(back)].sort((a, b) => a - b);
        const ratio = (values[1] + 0.05) / (values[0] + 0.05);
        const large = parseFloat(style.fontSize) >= 24 ||
          (parseFloat(style.fontSize) >= 18.667 && parseFloat(style.fontWeight) >= 700);
        const required = large ? 3 : 4.5;
        return ratio >= required ? [] : [{
          element: element.tagName.toLowerCase(),
          className: element.className,
          text: (element.textContent || element.getAttribute("aria-label") || "input").trim().slice(0, 80),
          color: style.color,
          background: back,
          ratio,
          required,
        }];
      });
    const controlFailures = Array.from(document.querySelectorAll<HTMLElement>(
      "input:not([type=hidden]):not([type=file]):not([type=checkbox]), select, textarea, .secondary, .primary, .page-button",
    ))
      .filter((element) => element.getClientRects().length > 0 && !element.matches(":disabled"))
      .flatMap((element) => {
        const style = getComputedStyle(element);
        const filled = element.matches(".primary, .page-button.current");
        const back = background(filled ? element.parentElement! : element);
        const border = over(parse(filled ? style.backgroundColor : style.borderTopColor), back);
        const values = [luminance(border), luminance(back)].sort((a, b) => a - b);
        const ratio = (values[1] + 0.05) / (values[0] + 0.05);
        return ratio >= 3 ? [] : [{
          element: element.tagName.toLowerCase(),
          className: element.className,
          text: "control border",
          color: style.borderTopColor,
          background: back,
          ratio,
          required: 3,
        }];
      });
    return [...textFailures, ...controlFailures];
  });
  expect(failures, "Visible text must meet WCAG AA contrast on its actual surface").toEqual([]);
}
