import { useState, useCallback, useEffect } from 'react'
import { storage } from '@/lib/storage'

export function useStrategyPool() {
  const [pool, setPool] = useState<string[]>(() => storage.strategyPool.get([]))

  // 同步写入 localStorage
  useEffect(() => { storage.strategyPool.set(pool) }, [pool])

  const addToPool = useCallback((id: string) => {
    setPool(prev => prev.includes(id) ? prev : [...prev, id])
  }, [])

  const removeFromPool = useCallback((id: string) => {
    setPool(prev => prev.filter(x => x !== id))
  }, [])

  const reorderPool = useCallback((newOrder: string[]) => {
    setPool(newOrder)
  }, [])

  const isInPool = useCallback((id: string) => pool.includes(id), [pool])

  return { pool, addToPool, removeFromPool, reorderPool, isInPool }
}
