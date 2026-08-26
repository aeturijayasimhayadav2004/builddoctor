import { useState } from "react";

import type { Diagnosis } from "./api.ts";
import LaneBadge from "./LaneBadge.tsx";
import MemoryCell from "./MemoryCell.tsx";
import { EMPTY, formatTime, orDash, percent, relativeTime } from "./format.ts";
import {
  ChevronRight,
  ExternalLink,
  FileText,
  Inbox,
  Sparkle,
  Terminal,
} from "./icons.tsx";

/**
 * Every diagnosis, newest first, with rows that open in place.
 *
 * Opening a row is state, not navigation - there is no url to share and no
 * router involved. A Set of open ids rather than a single id, so two rows
 * can be compared side by side without one closing the other.
 *
 * Each row is two <tr> elements inside their own <tbody>: the summary line
 * and the detail panel underneath it. Several tbody elements in one table
 * is valid HTML, and it keeps a row and its detail together instead of
 * relying on them happening to be adjacent.
 */
export default function DiagnosisTable({
  rows,
  filtered = false,
}: {
  rows: Diagnosis[];
  /** True when a lane filter is hiding rows, so the empty state can say so
   *  rather than claiming the database is empty. */
  filtered?: boolean;
}) {
  const [open, setOpen] = useState<ReadonlySet<number>>(new Set());

  function toggle(id: number) {
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  if (rows.length === 0) {
    return (
      <div className="empty">
        <Inbox size={28} />
        <p>
          {filtered
            ? "No diagnoses in this lane. Choose another lane, or clear the filter to see everything."
            : "No diagnoses yet. BuildDoctor writes a row here every time a watched build fails."}
        </p>
      </div>
    );
  }

  return (
    /* The table has eight columns that must not wrap, so below roughly a
       laptop width it stops fitting. This wrapper is what scrolls then -
       the page itself never scrolls sideways, which is the behaviour that
       makes a wide table tolerable instead of broken. */
    <div className="rows-scroll">
      <table className="rows">
        <thead>
          <tr>
            <th className="col-toggle" aria-label="Expand" />
            <th className="col-id">#</th>
            <th>Repository</th>
            <th>What BuildDoctor concluded</th>
            <th>Lane</th>
            <th>Memory</th>
            <th>Comment</th>
            <th>When</th>
          </tr>
        </thead>

        {rows.map((row) => {
          const isOpen = open.has(row.id);
          return (
            <tbody key={row.id} className={isOpen ? "row-group open" : "row-group"}>
              <tr
                className="summary-row"
                onClick={() => toggle(row.id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    toggle(row.id);
                  }
                }}
                tabIndex={0}
                role="button"
                aria-expanded={isOpen}
              >
                <td className="col-toggle">
                  {/* An SVG, not a text arrow. A glyph like the black
                      right-pointing triangle renders at a different size,
                      weight and vertical position in every font on every
                      platform, and there is no way to line it up with the
                      text next to it once and have it stay lined up. */}
                  <span className={isOpen ? "chevron down" : "chevron"}>
                    <ChevronRight size={15} />
                  </span>
                </td>
                <td className="col-id">{row.id}</td>
                <td className="col-repo">
                  {/* Only the repo name, not the owner. The owner is the same
                      on every row here and eats half the column. */}
                  {row.repo.split("/").at(-1) ?? row.repo}
                </td>
                <td className="col-summary" title={row.summary || undefined}>
                  {row.summary || EMPTY}
                </td>
                <td>
                  <LaneBadge lane={row.lane} />
                </td>
                <td>
                  <MemoryCell row={row} />
                </td>
                <td>
                  {/* Null for row 10, where a Phase 5 bug lost the url, and
                      for every amber run, which posts no comment at all. */}
                  {row.posted_url ? (
                    <a
                      className="link"
                      href={row.posted_url}
                      target="_blank"
                      rel="noreferrer"
                      onClick={(event) => event.stopPropagation()}
                    >
                      {row.posted_to === "pr_comment" ? "on the PR" : "on the commit"}
                      <ExternalLink size={12} />
                    </a>
                  ) : (
                    <span
                      className="muted"
                      title="No comment url was recorded for this run"
                    >
                      {EMPTY}
                    </span>
                  )}
                </td>
                <td className="col-when" title={formatTime(row.created_at)}>
                  {relativeTime(row.created_at)}
                </td>
              </tr>

              {isOpen && (
                <tr className="detail-row">
                  <td colSpan={8}>
                    <div className="detail">
                      <dl className="facts">
                        <div>
                          <dt>Workflow</dt>
                          <dd>{orDash(row.workflow)}</dd>
                        </div>
                        <div>
                          <dt>Failed step</dt>
                          <dd>{orDash(row.failed_step)}</dd>
                        </div>
                        <div>
                          <dt>Attempt</dt>
                          <dd className="num">{orDash(row.run_attempt)}</dd>
                        </div>
                        <div>
                          <dt>Run</dt>
                          <dd className="num">
                            {row.run_url ? (
                              <a href={row.run_url} target="_blank" rel="noreferrer">
                                {row.run_id}
                              </a>
                            ) : (
                              row.run_id
                            )}
                          </dd>
                        </div>
                        <div>
                          <dt>Files changed</dt>
                          <dd>
                            {row.files_changed.length > 0
                              ? row.files_changed.join(", ")
                              : EMPTY}
                          </dd>
                        </div>
                        <div>
                          <dt>Recorded</dt>
                          <dd>{formatTime(row.created_at)}</dd>
                        </div>
                      </dl>

                      {row.memory_match && (
                        <section className="panel panel-memory">
                          <h3>
                            <Sparkle size={12} />
                            Memory was used
                          </h3>
                          <p>
                            This diagnosis was written with diagnosis{" "}
                            <strong>#{row.memory_match.row_id}</strong> (run{" "}
                            {row.memory_match.run_id}, handled as{" "}
                            {orDash(row.memory_match.lane)}) supplied as context, at{" "}
                            <strong>{percent(row.memory_match.similarity)}</strong>{" "}
                            similarity.
                          </p>
                          <p className="note">
                            It was passed to the model as a hint it could use or
                            disregard, never as a fact. The lane above was decided
                            from this build's own log and diff.
                          </p>
                        </section>
                      )}

                      <section className="panel">
                        <h3>
                          <FileText size={12} />
                          Diagnosis
                        </h3>
                        <p className="prose">{row.diagnosis_text || EMPTY}</p>
                      </section>

                      <section className="panel">
                        <h3>
                          <Terminal size={12} />
                          Log excerpt
                        </h3>
                        <pre className="log">{row.log_excerpt || EMPTY}</pre>
                      </section>
                    </div>
                  </td>
                </tr>
              )}
            </tbody>
          );
        })}
      </table>
    </div>
  );
}
