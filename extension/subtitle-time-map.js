(() => {
  const MIN_ANCHOR_DISTANCE = 30;
  const MIN_RATE = 0.5;
  const MAX_RATE = 1.5;

  function createCalibrationMap(points) {
    if (!Array.isArray(points) || points.length !== 2 || points.some(point => !point)) return null;
    const anchors = points.map(point => ({
      playerTime: Number(point.playerTime),
      subtitleTime: Number(point.subtitleTime),
    }));
    if (anchors.some(point => !Number.isFinite(point.playerTime) || !Number.isFinite(point.subtitleTime) || point.playerTime < 0 || point.subtitleTime < 0)) return null;
    anchors.sort((left, right) => left.playerTime - right.playerTime);
    const playerSpan = anchors[1].playerTime - anchors[0].playerTime;
    if (playerSpan < MIN_ANCHOR_DISTANCE) return null;
    const rate = (anchors[1].subtitleTime - anchors[0].subtitleTime) / playerSpan;
    if (!Number.isFinite(rate) || rate < MIN_RATE || rate > MAX_RATE) return null;
    return {first: anchors[0], second: anchors[1], rate};
  }

  function playerTimeToSubtitleTime(points, playerTime) {
    const time = Number(playerTime);
    if (!Number.isFinite(time)) return playerTime;
    const map = createCalibrationMap(points);
    return map ? map.first.subtitleTime + (time - map.first.playerTime) * map.rate : time;
  }

  function subtitleTimeToPlayerTime(points, subtitleTime) {
    const time = Number(subtitleTime);
    if (!Number.isFinite(time)) return subtitleTime;
    const map = createCalibrationMap(points);
    return map ? map.first.playerTime + (time - map.first.subtitleTime) / map.rate : time;
  }

  globalThis.YTBA_SUBTITLE_TIME_MAP = Object.freeze({
    MIN_ANCHOR_DISTANCE,
    createCalibrationMap,
    playerTimeToSubtitleTime,
    subtitleTimeToPlayerTime,
  });
})();
