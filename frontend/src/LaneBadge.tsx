import type { Lane } from "./api.ts";
import { laneClass, laneLabel } from "./format.ts";

/**
 * The coloured lane pill. Teal, amber and coral are the same three names
 * this project has used for the lanes since Phase 4, so the page and the
 * README describe the same thing.
 *
 * A fourth, grey state exists for lane === null. Rows 1-4 were diagnosed
 * before lanes existed; showing them as one of the three would be a
 * quiet lie about what BuildDoctor did that day.
 *
 * The badge carries a dot AND a word. The dot is the fast read across a
 * column of rows; the word is what makes the lane survive a greyscale
 * screenshot, a projector with poor colour, or a reader who cannot
 * separate teal from amber. Colour alone would be the only signal, which
 * is the one thing a status indicator must never rely on.
 */
export default function LaneBadge({ lane }: { lane: Lane | null }) {
  return (
    <span
      className={laneClass(lane)}
      title={
        lane === null
          ? "Diagnosed before Phase 4 added lanes"
          : `Lane: ${laneLabel(lane)}`
      }
    >
      <span className="dot" aria-hidden="true" />
      {laneLabel(lane)}
    </span>
  );
}
