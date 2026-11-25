import { expect, test, type Page } from "@playwright/test";

export async function expectLayoutIntegrity(page: Page) {
  const failures = await page.evaluate(() => {
    const failures: { rule: string; element: string; detail: string }[] = [];
    const visible = (element: Element) => element.getClientRects().length > 0 &&
      getComputedStyle(element).visibility === "visible";
    const identify = (element: Element) => `${element.tagName.toLowerCase()}.${element.className}`;
    const fail = (rule: string, element: Element, detail: string) => {
      failures.push({ rule, element: identify(element), detail });
    };
    const panels = Array.from(document.querySelectorAll<HTMLElement>(
      ".job-card, .task-status, .template-summary, .facts-editor, .chat-panel, .pdf-reference, .draft-item, .source-list > li, .account-members > li, .account-settings form, .search-settings fieldset, .runtime-settings form, .notice, .confirmation-modal, .setup-guide, .setup-guide li, .message",
    )).filter(visible);
    for (const panel of panels) {
      if (panel.scrollWidth > panel.clientWidth + 1) {
        fail("panel-content-width", panel, `${panel.scrollWidth} > ${panel.clientWidth}`);
      }
    }
    const controls = Array.from(document.querySelectorAll<HTMLElement>(
      "main button, main a.button, main input:not([type=file]):not([type=hidden]), main select, main textarea, .confirmation-modal button",
    )).filter((element) => visible(element) && !element.closest(".sr-only"));
    for (const control of controls) {
      const bounds = control.getBoundingClientRect();
      const panel = control.closest<HTMLElement>(
        ".task-status, .facts-editor, .chat-panel, .pdf-reference, .source-list > li, .account-members > li, .account-settings form, .search-settings fieldset, .runtime-settings form, .notice, .confirmation-modal, .setup-guide li, .job-card",
      );
      if (panel) {
        const parent = panel.getBoundingClientRect();
        if (bounds.left < parent.left - 1 || bounds.right > parent.right + 1) {
          fail("control-contained", control, `${bounds.left.toFixed(1)}..${bounds.right.toFixed(1)} outside ${parent.left.toFixed(1)}..${parent.right.toFixed(1)}`);
        }
      }
      if (!control.matches("button, a.button")) continue;
      if (control.scrollWidth > control.clientWidth + 1) {
        fail("button-content-width", control, `${control.scrollWidth} > ${control.clientWidth}`);
      }
      const style = getComputedStyle(control);
      const walker = document.createTreeWalker(control, NodeFilter.SHOW_TEXT);
      let node: Node | null;
      while ((node = walker.nextNode())) {
        if (!node.textContent?.trim() || node.parentElement?.closest(".sr-only")) continue;
        const range = document.createRange();
        range.selectNodeContents(node);
        for (const text of range.getClientRects()) {
          if (text.width === 0) continue;
          const padding = [style.paddingLeft, style.paddingRight, style.paddingTop, style.paddingBottom].map(parseFloat);
          if (text.left < bounds.left + padding[0] - 1 || text.right > bounds.right - padding[1] + 1 ||
              text.top < bounds.top + padding[2] - 1 || text.bottom > bounds.bottom - padding[3] + 1) {
            fail("button-text-contained", control, node.textContent.trim().slice(0, 80));
          }
        }
        if (control.matches(".setup-guide .button")) {
          for (const word of node.textContent.matchAll(/\p{L}+/gu)) {
            range.setStart(node, word.index!);
            range.setEnd(node, word.index! + word[0].length);
            if (Array.from(range.getClientRects()).filter((rect) => rect.width > 0).length > 1) {
              fail("setup-button-word-wrap", control, word[0]);
            }
          }
        }
      }
      for (const svg of control.querySelectorAll("svg")) {
        if (Number(svg.getAttribute("width")) >= 16 && svg.getBoundingClientRect().width < 15.5) {
          fail("button-icon-size", control, String(svg.getBoundingClientRect().width));
        }
      }
    }
    for (const selector of [".job-actions", ".source-actions", ".editor-actions", ".confirmation-actions", ".download-row", ".task-status", ".task-delete-confirmation > div"]) {
      for (const group of document.querySelectorAll(selector)) {
        const items = Array.from(group.querySelectorAll<HTMLElement>("button, a")).filter(visible);
        for (let i = 0; i < items.length; i++) {
          for (let j = i + 1; j < items.length; j++) {
            const a = items[i].getBoundingClientRect();
            const b = items[j].getBoundingClientRect();
            if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1 &&
                Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1) {
              fail("controls-overlap", group, `${items[i].textContent?.trim()} / ${items[j].textContent?.trim()}`);
            }
            if (Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1) {
              const gap = Math.max(a.left, b.left) - Math.min(a.right, b.right);
              if (gap >= 0 && gap < 5) fail("control-spacing", group, String(gap));
            }
          }
        }
      }
    }
    const titlePairs = [
      [".setup-guide", ":scope > h2"],
      [".search-settings fieldset", ":scope > legend"],
      [".account-settings form", ":scope > h2"],
      [".runtime-settings form", ":scope > h2"],
      [".facts-editor", ":scope > header h2"],
      [".chat-panel", ".chat-heading h3"],
      [".source-list > li", ":scope > h3"],
    ];
    const topbar = document.querySelector(".topbar");
    const caption = topbar?.querySelector<HTMLElement>(":scope > span");
    const account = topbar?.querySelector<HTMLElement>(".topbar-account");
    const main = document.querySelector<HTMLElement>(".main-content");
    const avatar = document.querySelector(".owner-avatar");
    if (avatar && visible(avatar)) {
      const bounds = avatar.getBoundingClientRect();
      if (bounds.width < 29.5 || Math.abs(bounds.width - bounds.height) > 1) {
        fail("avatar-size", avatar, `${bounds.width.toFixed(1)} x ${bounds.height.toFixed(1)}`);
      }
    }
    if (caption && account && main && visible(caption)) {
      const gap = account.getBoundingClientRect().left - caption.getBoundingClientRect().right;
      if (gap < 15) fail("header-content-gap", caption, String(gap));
      const expected = main.getBoundingClientRect().left + parseFloat(getComputedStyle(main).paddingLeft);
      if (Math.abs(caption.getBoundingClientRect().left - expected) > 1) {
        fail("header-page-alignment", caption, `${caption.getBoundingClientRect().left} != ${expected}`);
      }
    }
    for (const [panelSelector, titleSelector] of titlePairs) {
      for (const panel of document.querySelectorAll<HTMLElement>(panelSelector)) {
        const title = panel.querySelector<HTMLElement>(titleSelector);
        if (!title || !visible(title)) continue;
        const expected = panel.getBoundingClientRect().left + parseFloat(getComputedStyle(panel).paddingLeft) + parseFloat(getComputedStyle(panel).borderLeftWidth);
        if (Math.abs(title.getBoundingClientRect().left - expected) > 1) {
          fail("panel-title-alignment", title, `${title.getBoundingClientRect().left.toFixed(1)} != ${expected.toFixed(1)}`);
        }
      }
    }
    for (const form of document.querySelectorAll<HTMLElement>(".account-settings form, .runtime-settings form, .search-settings fieldset")) {
      if (!visible(form)) continue;
      const style = getComputedStyle(form);
      if (parseFloat(style.paddingLeft) < 16 || Math.abs(parseFloat(style.paddingLeft) - parseFloat(style.paddingRight)) > 1) {
        fail("form-padding", form, `${style.paddingLeft} / ${style.paddingRight}`);
      }
      const fields = Array.from(form.querySelectorAll<HTMLElement>(":scope > label"))
        .map((label) => ({ label, input: label.querySelector<HTMLElement>(":scope > input:not([type=checkbox]), :scope > select, :scope > .settings-select > select") }))
        .filter((field) => field.input && visible(field.input));
      for (let i = 0; i < fields.length; i++) {
        for (let j = i + 1; j < fields.length; j++) {
          if (Math.abs(fields[i].label.getBoundingClientRect().top - fields[j].label.getBoundingClientRect().top) > 1) continue;
          const a = fields[i].input!.getBoundingClientRect();
          const b = fields[j].input!.getBoundingClientRect();
          if (Math.abs(a.top - b.top) > 1 || Math.abs(a.bottom - b.bottom) > 1) {
            fail("form-row-alignment", form, `${a.top.toFixed(1)}..${a.bottom.toFixed(1)} / ${b.top.toFixed(1)}..${b.bottom.toFixed(1)}`);
          }
        }
      }
    }
    return failures;
  });
  if (failures.length) {
    await test.info().attach(`layout-${page.viewportSize()?.width}`, {
      contentType: "application/json",
      body: JSON.stringify(failures, null, 2),
    });
  }
  expect.soft(failures, "Content must fit its panel and controls without overlap").toEqual([]);
}
