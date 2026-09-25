import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../extension/subtitle-time-map.js", import.meta.url), "utf8");
const context = {};
context.globalThis = context;
vm.runInNewContext(source, context);
const timeMap = context.YTBA_SUBTITLE_TIME_MAP;

test("two anchors map player time to subtitle time and invert transcript seeks", () => {
  const points = [
    {playerTime: 300, subtitleTime: 297},
    {playerTime: 600, subtitleTime: 594},
  ];

  assert.equal(timeMap.playerTimeToSubtitleTime(points, 450), 445.5);
  assert.ok(Math.abs(timeMap.subtitleTimeToPlayerTime(points, 445.5) - 450) < 1e-9);
});

test("anchor order does not change the fitted linear mapping", () => {
  const points = [
    {playerTime: 600, subtitleTime: 594},
    {playerTime: 300, subtitleTime: 297},
  ];

  assert.equal(timeMap.playerTimeToSubtitleTime(points, 450), 445.5);
});

test("missing, close, invalid, or reverse-time anchors leave the clock unchanged", () => {
  assert.equal(timeMap.playerTimeToSubtitleTime([], 120), 120);
  assert.equal(timeMap.playerTimeToSubtitleTime([
    {playerTime: 100, subtitleTime: 99},
    {playerTime: 120, subtitleTime: 119},
  ], 110), 110);
  assert.equal(timeMap.playerTimeToSubtitleTime([
    {playerTime: 100, subtitleTime: 110},
    {playerTime: 200, subtitleTime: 90},
  ], 150), 150);
  assert.equal(timeMap.createCalibrationMap([
    {playerTime: 100, subtitleTime: 99},
    {playerTime: 200, subtitleTime: 90},
  ]), null);
});
