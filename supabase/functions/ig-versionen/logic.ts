// Regeln fuer gleichzeitige Klicks — ohne Netz, damit sie testbar sind (logic_test.ts).

export interface JobScope {
  id?: number;
  serial: string | null;
  package: string | null;
  status?: string;
}

/** Deckt Auftrag `job` (Mac/Phone/App) den Bereich serial/pkg ab? */
export function covers(job: JobScope, serial: string | null, pkg: string | null): boolean {
  if (job.serial === null) return true;
  if (job.serial !== serial) return false;
  return job.package === null || job.package === pkg;
}

/**
 * Offener Auftrag, der den neuen ueberfluessig macht:
 *   - derselbe Bereich (wartend oder laufend), oder
 *   - ein WARTENDER Phone-Auftrag, der diese App mit erledigt.
 * Ein Mac-Auftrag zaehlt nicht (App/Phone haben Vorrang vor ihm), ein laufender
 * Phone-Auftrag auch nicht (er kann an dieser App schon vorbei sein).
 */
export function findCovering(open: JobScope[], serial: string | null, pkg: string | null): JobScope | undefined {
  return open.find((j) =>
    (j.serial === serial && j.package === pkg) ||
    (j.status === "queued" && j.serial !== null && covers(j, serial, pkg)));
}

/** Wartende Auftraege, die im neuen aufgehen: nur App-Auftraege im neuen Phone-Auftrag. */
export function toMerge(open: JobScope[], job: JobScope): JobScope[] {
  if (job.serial === null) return [];
  return open.filter((j) => j.status === "queued" && j.id !== job.id && covers(job, j.serial, j.package));
}
