import { useEffect, useRef } from "react";

export function useRefreshEffect(
  load: (signal: AbortSignal) => Promise<void>,
  revision: number,
) {
  const refresh = useRef<() => Promise<void>>(null);
  useEffect(() => {
    const controller = new AbortController();
    let running = false;
    let pending = false;
    refresh.current = async () => {
      if (running) {
        pending = true;
        return;
      }
      running = true;
      try {
        do {
          pending = false;
          await load(controller.signal);
        } while (pending && !controller.signal.aborted);
      } finally {
        running = false;
      }
    };
    return () => {
      refresh.current = null;
      controller.abort();
    };
  }, [load]);
  useEffect(() => {
    void refresh.current?.();
  }, [load, revision]);
}
