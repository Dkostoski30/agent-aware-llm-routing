import { computed, signal } from '@angular/core';
import { RunFile, readRunFile } from '../runs/run-files';

/** The files a composer holds until its message is sent (P2.6a), for the empty chat and an open one alike. Each
 * screen makes its own, so a draft's files never follow the user to another chat. */
export class Attachments {
  private readonly held = signal<RunFile[]>([]);
  private readonly busy = signal(false);
  readonly files = this.held.asReadonly();
  /** The names of the files, in the order they were attached. */
  readonly names = computed(() => this.held().map((file) => file.name));
  /** A pick is still being read: a send now would leave its files out of the message. Attach is disabled meanwhile,
   * so picks never overlap. */
  readonly reading = this.busy.asReadonly();

  /** Reads the picked files and keeps the ones it can; why the others were refused, or undefined. */
  async add(picked: File[]): Promise<string | undefined> {
    this.busy.set(true);
    try {
      const read = await Promise.all(picked.map(readRunFile));
      const files: RunFile[] = [];
      const refused: string[] = [];
      for (const result of read) {
        if ('refused' in result) refused.push(result.refused);
        else files.push(result);
      }
      this.held.update((attached) => [...attached, ...files]);
      return refused.length > 0 ? refused.join('; ') : undefined;
    } finally {
      this.busy.set(false);
    }
  }

  remove(position: number): void {
    this.held.update((files) => files.filter((_, index) => index !== position));
  }

  clear(): void {
    this.held.set([]);
  }
}
