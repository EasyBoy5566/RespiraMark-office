(function (global) {
  "use strict";

  // 警報音的唯一音符表：[開始秒數, 持續秒數, 頻率 Hz]。
  // Level 1 兩組的第 5 音是下方兩個 785 Hz；想再拉長尾音時，只需調整其 duration。
  const PATTERNS = {
    1: [
      { at: 0.014, duration: 0.150, hz: 988 },
      { at: 0.184, duration: 0.150, hz: 988 },
      { at: 0.353, duration: 0.200, hz: 988 },
      { at: 0.673, duration: 0.150, hz: 988 },
      { at: 0.842, duration: 0.200, hz: 785 }, // 第一組第 5 音
      { at: 2.042, duration: 0.150, hz: 988 },
      { at: 2.212, duration: 0.150, hz: 988 },
      { at: 2.381, duration: 0.200, hz: 988 },
      { at: 2.701, duration: 0.150, hz: 988 },
      { at: 2.870, duration: 0.200, hz: 785 }, // 第二組第 5 音
    ],
    2: [
      { at: 0.015, duration: 0.170, hz: 988 },
      { at: 0.264, duration: 0.170, hz: 988 },
      { at: 0.483, duration: 0.200, hz: 785 },
    ],
    3: [
      { at: 0.000, duration: 0.180, hz: 988 },
      { at: 0.248, duration: 0.180, hz: 988 },
    ],
  };

  const LEVEL_GAIN = { 1: 0.24, 2: 0.21, 3: 0.23 };

  function patternFor(level) {
    return PATTERNS[Number(level)] || PATTERNS[3];
  }

  function duration(level) {
    return Math.max(...patternFor(level).map((note) => note.at + note.duration));
  }

  function play(context, level, startAt, destination) {
    const numericLevel = Number(level);
    const pattern = patternFor(numericLevel);
    const output = destination || context.destination;
    const at = Number.isFinite(startAt) ? startAt : context.currentTime;
    const nodes = new Set();

    for (const note of pattern) {
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      const start = at + note.at;
      const end = start + note.duration;
      const entry = { oscillator, gain };

      oscillator.type = "sine";
      oscillator.frequency.setValueAtTime(note.hz, start);
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(LEVEL_GAIN[numericLevel] || 0.2, start + 0.008);
      gain.gain.setValueAtTime(
        LEVEL_GAIN[numericLevel] || 0.2,
        Math.max(start + 0.008, end - 0.012),
      );
      gain.gain.exponentialRampToValueAtTime(0.0001, end);
      oscillator.connect(gain);
      gain.connect(output);
      nodes.add(entry);
      oscillator.start(start);
      oscillator.stop(end);
      oscillator.onended = () => {
        nodes.delete(entry);
        oscillator.disconnect();
        gain.disconnect();
      };
    }

    const totalDuration = duration(numericLevel);
    return {
      duration: totalDuration,
      endAt: at + totalDuration,
      stop() {
        for (const entry of nodes) {
          entry.oscillator.onended = null;
          try { entry.oscillator.stop(); } catch (error) { /* 已自然結束 */ }
          try { entry.oscillator.disconnect(); } catch (error) { /* 已斷開 */ }
          try { entry.gain.disconnect(); } catch (error) { /* 已斷開 */ }
        }
        nodes.clear();
      },
    };
  }

  global.RMAlarmSynth = Object.freeze({ PATTERNS, LEVEL_GAIN, duration, play });
})(window);
