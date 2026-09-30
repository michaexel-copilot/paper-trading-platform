import { useEffect, useState } from 'react'

export default function App() {
  const [health, setHealth] = useState('checking…')

  useEffect(() => {
    fetch('/api/health')
      .then((r) => r.json())
      .then((body) => setHealth(body.status))
      .catch(() => setHealth('unreachable'))
  }, [])

  return <p>API health: {health}</p>
}
