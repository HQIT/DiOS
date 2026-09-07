#!/usr/bin/env node
/** 录制 title-dios.svg 动效为 webm（供 01-title 分镜使用） */
import { chromium } from "playwright";
import { mkdir, readdir, rename, rm } from "fs/promises";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const HTML = join(ROOT, "docs/video-assets/svg/title-dios.html");
const CACHE = join(ROOT, "docs/video-assets/segments/.cache");
const OUT = join(CACHE, "01-title-animated.webm");

const DURATION_MS = parseInt(process.env.TITLE_ANIM_MS || "12000", 10);

async function main() {
  const tmp = join(CACHE, `rec-${Date.now()}`);
  await mkdir(tmp, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    recordVideo: { dir: tmp, size: { width: 1920, height: 1080 } },
  });
  const page = await context.newPage();
  await page.goto(`file://${HTML}`, { waitUntil: "load" });
  await page.waitForTimeout(DURATION_MS);
  await context.close();
  await browser.close();

  const webm = (await readdir(tmp)).find((f) => f.endsWith(".webm"));
  if (!webm) throw new Error("录制失败：未生成 webm");
  await mkdir(CACHE, { recursive: true });
  await rm(OUT, { force: true });
  await rename(join(tmp, webm), OUT);
  await rm(tmp, { recursive: true, force: true });
  console.log(OUT);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
