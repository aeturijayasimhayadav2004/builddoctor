/**
 * What the page shows while the first request is still in the air.
 *
 * The word "Loading" tells the reader nothing except that they are
 * waiting. A block roughly the size and position of the thing that is
 * about to arrive tells them what is coming, and - the part that actually
 * matters - occupies the same space, so nothing jumps when the real
 * content replaces it.
 *
 * Only shown on the FIRST load. A refresh leaves the existing rows on
 * screen, because replacing real data with grey boxes to fetch data that
 * is probably identical is a downgrade.
 */

/** One shimmering bar. Width is a percentage so it flexes with the layout. */
function Line({ width, height = 12 }: { width: string; height?: number }) {
  return <div className="skeleton sk-line" style={{ width, height }} />;
}

export function CardsSkeleton() {
  return (
    <section className="cards" aria-hidden="true">
      {[0, 1, 2, 3].map((i) => (
        <div className="sk-card" key={i}>
          <Line width="45%" height={10} />
          <Line width="35%" height={26} />
          <Line width="80%" height={10} />
        </div>
      ))}
    </section>
  );
}

export function TableSkeleton() {
  return (
    <div className="sk-rows" aria-hidden="true">
      {/* Widths vary per row on purpose. Eight identical bars read as a
          rendering glitch; uneven ones read as text that has not arrived. */}
      {["78%", "64%", "71%", "58%", "82%", "67%"].map((width, i) => (
        <Line width={width} key={i} />
      ))}
    </div>
  );
}

/**
 * The polite version of the same thing for screen readers, which get no
 * benefit at all from grey rectangles.
 */
export function LoadingAnnouncement() {
  return (
    <p role="status" aria-live="polite" className="sr-only">
      Loading diagnoses…
    </p>
  );
}
