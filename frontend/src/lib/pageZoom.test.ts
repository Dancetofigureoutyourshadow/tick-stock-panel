// @vitest-environment jsdom
import { afterEach, expect, it } from 'vitest'
import { applyPageZoom } from './pageZoom'

afterEach(() => {
  document.documentElement.style.zoom = ''
  document.body.innerHTML = ''
})

it('uses a transformed layout without CSS zoom so chart pointer coordinates stay local', () => {
  document.body.innerHTML = '<div id="root"></div>'
  // A running page may still have the legacy inline zoom during an update.
  document.documentElement.style.zoom = '0.9'
  expect(applyPageZoom(0.9)).toBe(0.9)
  expect(document.documentElement.style.zoom).toBe('')
  const root = document.getElementById('root')!
  expect(root.style.transform).toBe('scale(0.9)')
  expect(parseFloat(root.style.width)).toBeCloseTo(100 / 0.9)
  applyPageZoom(1)
  expect(root.style.transform).toBe('')
  expect(root.style.width).toBe('')
  expect(root.style.height).toBe('')
})
