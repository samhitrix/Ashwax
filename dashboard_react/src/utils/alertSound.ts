// Web Audio beep for CRITICAL/EMERGENCY system alerts — no audio asset files, so
// nothing to fail to load. The backend (src/infrastructure/alert_system.py
// TIER_POLICY) already models an `audio` policy per tier ("alarm" for CRITICAL,
// "siren" for EMERGENCY) but nothing in the dashboard ever played anything —
// alerts were visual-only. This is the missing playback half of that contract.
//
// Browsers block audio until the page has had a user gesture (click/keypress).
// resume() is attempted defensively but will silently no-op before that first
// gesture — a browser platform constraint, not something this module can bypass.

let _audioCtx: AudioContext | null = null;

function getAudioContext(): AudioContext | null {
  try {
    if (!_audioCtx) {
      const Ctor = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!Ctor) return null;
      _audioCtx = new Ctor();
    }
    if (_audioCtx.state === 'suspended') {
      void _audioCtx.resume().catch(() => {});
    }
    return _audioCtx;
  } catch {
    return null;
  }
}

function beep(ctx: AudioContext, freq: number, startAt: number, duration: number): void {
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = 'square';
  osc.frequency.value = freq;
  gain.gain.setValueAtTime(0.0001, startAt);
  gain.gain.exponentialRampToValueAtTime(0.25, startAt + 0.02);
  gain.gain.exponentialRampToValueAtTime(0.0001, startAt + duration);
  osc.connect(gain);
  gain.connect(ctx.destination);
  osc.start(startAt);
  osc.stop(startAt + duration + 0.05);
}

/** Plays the alarm (CRITICAL) or siren (EMERGENCY) tone for a newly-raised alert. */
export function playAlertSound(tier: 'CRITICAL' | 'EMERGENCY'): void {
  const ctx = getAudioContext();
  if (!ctx) return;
  const now = ctx.currentTime;
  if (tier === 'EMERGENCY') {
    // Three urgent high-low sweeps — deliberately harder to ignore than CRITICAL.
    [0, 0.28, 0.56].forEach((offset) => {
      beep(ctx, 1200, now + offset, 0.14);
      beep(ctx, 800, now + offset + 0.14, 0.1);
    });
  } else {
    // Two sharp beeps.
    beep(ctx, 880, now, 0.15);
    beep(ctx, 880, now + 0.25, 0.15);
  }
}
