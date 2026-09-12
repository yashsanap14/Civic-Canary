import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'

import App from './App'

afterEach(cleanup)

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    const body = url.endsWith('/api/targets')
      ? [{ target_id: 'benefits-demo', name: 'River County Benefits Portal', active_version: 'v1', enabled: true }]
      : []
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }))
})

test('renders the decision dashboard without persisting a token', async () => {
  render(<App />)
  expect(await screen.findByRole('option', { name: 'River County Benefits Portal' })).toBeInTheDocument()
  expect(screen.getByLabelText('Review token')).toHaveAttribute('type', 'password')
  expect(screen.getByRole('button', { name: /run canary scan/i })).toBeDisabled()
})



test('adds a public website with objective and scan frequency', async () => {
  render(<App />)
  await screen.findByRole('option', { name: 'River County Benefits Portal' })
  fireEvent.change(screen.getByLabelText('Review token'), { target: { value: 'review-demo' } })
  fireEvent.click(screen.getByRole('button', { name: 'Add Website' }))
  fireEvent.change(screen.getByLabelText('Website name'), { target: { value: 'Library delivery' } })
  fireEvent.change(screen.getByLabelText('Public HTTPS URL'), { target: { value: 'https://library.example.org' } })
  fireEvent.change(screen.getByLabelText('What should we monitor?'), { target: { value: 'Delivery availability' } })
  fireEvent.click(screen.getByRole('button', { name: 'Inspect website' }))
  await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/targets'), expect.objectContaining({
    method: 'POST', body: expect.stringContaining('Delivery availability'),
  })))
})
