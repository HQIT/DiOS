#!/usr/bin/env node
/**
 * Playwright 录制 DiOS 视频素材 S-07/08/10/11
 * 输出: docs/video-assets/recordings/*.webm
 */
import { chromium } from "playwright";
import { mkdir, readdir, rename, unlink } from "fs/promises";
import { join, dirname } from "path";
import { fileURLToPath } from "url";
import { execSync } from "child_process";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const BASE = process.env.DIOS_UI_BASE || "http://127.0.0.1:3001/dios";
const OUT = join(ROOT, "docs/video-assets/recordings");

async function finalizeVideo(context, videoDir, targetName) {
  await context.close();
  const files = await readdir(videoDir);
  const webm = files.find((f) => f.endsWith(".webm"));
  if (!webm) throw new Error(`未找到录制文件: ${targetName}`);
  const dest = join(OUT, targetName);
  await rename(join(videoDir, webm), dest);
  return dest;
}

async function record(name, fn) {
  await mkdir(OUT, { recursive: true });
  const videoDir = join(OUT, `.tmp-${name.replace(/\.webm$/, "")}`);
  await mkdir(videoDir, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    recordVideo: { dir: videoDir, size: { width: 1920, height: 1080 } },
    locale: "zh-CN",
  });
  const page = await context.newPage();
  try {
    await fn(page);
  } finally {
    const path = await finalizeVideo(context, videoDir, name);
    await browser.close();
    console.log(`✓ ${name} -> ${path}`);
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  // S-08: Console <-> Chat
  await record("S-08-app-shell-switch.webm", async (page) => {
    await page.goto(`${BASE}/#/console/agents`, { waitUntil: "networkidle" });
    await sleep(1500);
    await page.getByRole("button", { name: "Chat" }).click();
    await sleep(2500);
    await page.getByRole("button", { name: "Console" }).click();
    await sleep(2000);
    await page.getByRole("button", { name: "Chat" }).click();
    await sleep(1500);
  });

  // S-07: Events logs + expand first row
  await record("S-07-console-event-logs.webm", async (page) => {
    await page.goto(`${BASE}/#/console/events`, { waitUntil: "networkidle" });
    await sleep(2000);
    const row = page.locator(".event-row").first();
    if (await row.count()) {
      await row.click();
      await sleep(3500);
      await row.click();
      await sleep(1000);
      await row.click();
      await sleep(3000);
    }
    await page.getByRole("button", { name: "活动总览" }).click();
    await sleep(3000);
    await page.getByRole("button", { name: "事件日志" }).click();
    await sleep(1500);
  });

  // S-10: Chat streaming (SimpleChatBot)
  await record("S-10-chat-streaming.webm", async (page) => {
    await page.goto(`${BASE}/#/chat`, { waitUntil: "networkidle" });
    await sleep(2000);
    const bot = page.getByText("SimpleChatBot", { exact: false });
    if (await bot.count()) await bot.first().click();
    await sleep(1000);
    const ta = page.locator("textarea").first();
    await ta.fill("用一句话介绍 DiOS");
    await sleep(500);
    await page.getByRole("button", { name: "Send" }).click();
    await sleep(12000);
  });

  // S-11: session switch (if any)
  await record("S-11-chat-sessions.webm", async (page) => {
    await page.goto(`${BASE}/#/chat`, { waitUntil: "networkidle" });
    await sleep(2000);
    const sessions = page.locator('[class*="session"], .chat-session, aside button').filter({ hasText: /.+/ });
    const items = page.locator("aside").getByRole("button");
    const n = await items.count();
    if (n >= 2) {
      await items.nth(1).click();
      await sleep(2500);
      await items.nth(0).click();
      await sleep(2500);
      if (n >= 3) await items.nth(2).click();
      await sleep(2000);
    } else {
      const ta = page.locator("textarea").first();
      await ta.fill("第二条会话测试");
      await page.getByRole("button", { name: "Send" }).click();
      await sleep(8000);
      await page.getByRole("button", { name: "New", exact: false }).click().catch(() => {});
      await sleep(1500);
    }
  });
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
