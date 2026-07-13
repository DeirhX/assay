/** Drain microtasks between DOM updates and async view loaders in Vitest. */
export async function flushPromises(): Promise<void> {
  for (let i = 0; i < 6; i++) await Promise.resolve();
}
