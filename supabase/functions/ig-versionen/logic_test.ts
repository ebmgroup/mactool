import { assertEquals } from "jsr:@std/assert@1";
import { covers, findCovering, toMerge } from "./logic.ts";

const app = (id: number, serial: string, pkg: string, status = "queued") => ({ id, serial, package: pkg, status });
const phone = (id: number, serial: string, status = "queued") => ({ id, serial, package: null, status });
const mac = (id: number, status = "queued") => ({ id, serial: null, package: null, status });

Deno.test("covers: Mac > Phone > App", () => {
  assertEquals(covers(mac(1), "A", "x"), true);
  assertEquals(covers(phone(1, "A"), "A", "x"), true);
  assertEquals(covers(phone(1, "A"), "B", "x"), false);
  assertEquals(covers(app(1, "A", "x"), "A", "y"), false);
});

Deno.test("Doppelklick auf dieselbe App -> derselbe Auftrag", () => {
  assertEquals(findCovering([app(5, "A", "x")], "A", "x")?.id, 5);
  assertEquals(findCovering([app(5, "A", "x", "running")], "A", "x")?.id, 5);
});

Deno.test("wartendes Phone deckt die App ab, laufendes nicht", () => {
  assertEquals(findCovering([phone(6, "A")], "A", "x")?.id, 6);
  assertEquals(findCovering([phone(6, "A", "running")], "A", "x"), undefined);
});

Deno.test("Mac-Auftrag deckt nichts ab: App/Phone bekommen eigenen Auftrag mit Vorrang", () => {
  assertEquals(findCovering([mac(7)], "A", "x"), undefined);
  assertEquals(findCovering([mac(7)], "A", null), undefined);
});

Deno.test("Phone-Auftrag uebernimmt wartende App-Auftraege desselben Phones", () => {
  const open = [app(1, "A", "x"), app(2, "A", "y", "running"), app(3, "B", "x"), mac(4)];
  assertEquals(toMerge(open, phone(9, "A")).map((j) => j.id), [1]);
});

Deno.test("Mac-Auftrag uebernimmt nichts", () => {
  assertEquals(toMerge([app(1, "A", "x"), phone(2, "B")], mac(9)), []);
});
