/*
 * Drive-pad direction maths, kept in ONE place so it can be unit-tested.
 *
 * Convention (this is what bit us once): dx/dy are normalised offsets from the
 * pad centre in SCREEN space, so dy is POSITIVE DOWNWARDS - a finger above the
 * centre means dy < 0 and therefore FORWARD. The old inline code negated dy
 * when reading the pointer and negated it again when deriving the direction,
 * which cancelled out into "up means reverse".
 */
(function (root) {
  var DEADZONE = 0.22;

  function axisFlags(dx, dy, dz) {
    if (dz === undefined) dz = DEADZONE;
    return {
      forward: dy < -dz,
      backward: dy > dz,
      left: dx < -dz,
      right: dx > dz
    };
  }

  /* Keep the knob on the pad: scale a vector longer than 1 back onto the rim. */
  function clampVec(dx, dy) {
    var m = Math.hypot(dx, dy);
    if (m > 1) { dx /= m; dy /= m; }
    return [dx, dy];
  }

  /* Pad and keyboard are independent sources; either one drives. */
  function mergeFlags(pad, key) {
    return {
      forward: pad.forward || !!key.w,
      backward: pad.backward || !!key.s,
      left: pad.left || !!key.a,
      right: pad.right || !!key.d
    };
  }

  root.QPAD = { axisFlags: axisFlags, clampVec: clampVec, mergeFlags: mergeFlags, DEADZONE: DEADZONE };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.QPAD;
})(typeof window !== 'undefined' ? window : globalThis);
