import { useState, useCallback } from 'react';

const STORAGE_KEY = 'pt_point_multiplier';

export function usePointMultiplier() {
  const [multiplier, setMultiplierState] = useState<number>(() => {
    const saved = localStorage.getItem(STORAGE_KEY);
    return saved ? Number(saved) : 4;
  });

  const setMultiplier = useCallback((val: number) => {
    const clamped = Math.max(1, Math.min(100, val));
    setMultiplierState(clamped);
    localStorage.setItem(STORAGE_KEY, String(clamped));
  }, []);

  return { multiplier, setMultiplier };
}
