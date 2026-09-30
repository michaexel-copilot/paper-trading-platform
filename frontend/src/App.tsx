import { Navigate, NavLink, Outlet, Route, Routes, useNavigate } from 'react-router'
import { useSession, useUser } from './auth'
import RequireAuth from './components/RequireAuth'
import AssetPage from './pages/AssetPage'
import AssetsPage from './pages/AssetsPage'
import LoginPage from './pages/LoginPage'
import PortfolioPage from './pages/PortfolioPage'
import PortfoliosPage from './pages/PortfoliosPage'

function Layout() {
  const { data: user } = useUser()
  const session = useSession()
  const navigate = useNavigate()

  async function signOut() {
    await session.signOut()
    navigate('/login')
  }

  return (
    <>
      <header className="topbar">
        <span className="brand">Paper Trading</span>
        <nav>
          <NavLink to="/portfolios">Portfolios</NavLink>
          <NavLink to="/assets">Assets</NavLink>
        </nav>
        <span className="spacer" />
        <span className="muted">{user?.email}</span>
        <button className="link" onClick={signOut}>
          Sign out
        </button>
      </header>
      <main>
        <Outlet />
      </main>
      <footer className="muted">
        Simulation only. No real orders are placed. Prices from OKX and other exchanges via ccxt,
        and from Yahoo Finance.
      </footer>
    </>
  )
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route path="/portfolios" element={<PortfoliosPage />} />
        <Route path="/portfolios/:portfolioId" element={<PortfolioPage />} />
        <Route path="/assets" element={<AssetsPage />} />
        <Route path="/assets/:assetId" element={<AssetPage />} />
        <Route path="*" element={<Navigate to="/portfolios" replace />} />
      </Route>
    </Routes>
  )
}
