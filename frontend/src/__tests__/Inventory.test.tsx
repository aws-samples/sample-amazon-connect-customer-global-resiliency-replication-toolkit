import { render } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import fc from "fast-check";
import ResourceTable from "../components/Inventory/ResourceTable";
import { ResourceType, ReplicationStatus, type Resource } from "../types";

function makeResources(n: number): Resource[] {
  const out: Resource[] = [];
  for (let i = 0; i < n; i++) {
    out.push({
      id: `res-${i}`,
      name: `res-${i}`,
      arn: `arn:aws:lambda:us-east-1:000000000000:function:res-${i}`,
      resource_type: ResourceType.LAMBDA,
      status: ReplicationStatus.NOT_REPLICATED,
      config_summary: {},
      dependencies: [],
    });
  }
  return out;
}

function countDataRows(container: HTMLElement): number {
  // Cloudscape Table renders data rows inside <tbody>. Header rows are in <thead>.
  // When the table is empty, Cloudscape renders a single "empty-state" row whose
  // <td> has colSpan === totalColumnsCount (a merged cell). We must exclude
  // that row from the data-row count. A real data row has individual <td>
  // cells without colspan > 1.
  const tbodyRows = container.querySelectorAll("tbody tr");
  let count = 0;
  tbodyRows.forEach((row) => {
    const tds = row.querySelectorAll("td");
    if (tds.length === 0) return;
    // Skip the merged empty-state row (single td spanning all columns).
    const isEmptyStateRow = Array.from(tds).some(
      (td) => (td.getAttribute("colspan") ?? "1") !== "1",
    );
    if (!isEmptyStateRow) count++;
  });
  return count;
}

/**
 * Validates: Requirements 3.5, 3.6 (Property 6 — inventory pagination row-count invariant)
 */
describe("ResourceTable pagination (Property 6)", () => {
  it("first-page row count equals min(N, 25) (property over N ∈ [0, 500])", () => {
    fc.assert(
      fc.property(fc.nat({ max: 500 }), (n) => {
        const resources = makeResources(n);
        const { container, unmount } = render(
          <ResourceTable
            resources={resources}
            selectedIds={new Set()}
            onSelectionChange={vi.fn()}
          />,
        );
        try {
          const rows = countDataRows(container);
          expect(rows).toBe(Math.min(n, 25));
        } finally {
          unmount();
        }
      }),
      { numRuns: 25 },
    );
  });

  for (const n of [0, 1, 24, 25, 26, 100, 500]) {
    it(`first-page has min(${n}, 25) = ${Math.min(n, 25)} data rows`, () => {
      const { container, unmount } = render(
        <ResourceTable
          resources={makeResources(n)}
          selectedIds={new Set()}
          onSelectionChange={vi.fn()}
        />,
      );
      try {
        expect(countDataRows(container)).toBe(Math.min(n, 25));
      } finally {
        unmount();
      }
    });
  }
});
