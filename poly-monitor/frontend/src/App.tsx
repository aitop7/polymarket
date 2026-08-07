import { NavLink, Outlet } from 'react-router-dom'

export default function App() {
  return (
    <div className="app-shell">
      <header className="topnav">
        <div className="brand">
          poly<span>-monitor</span>
        </div>
        <nav className="nav-links">
          <NavLink to="/" end>
            Monitor
          </NavLink>
          <NavLink to="/paper">Paper</NavLink>
          <NavLink to="/backtest">Backtest</NavLink>
        </nav>
      </header>
      <main className="main">
        <Outlet />
      </main>
    </div>
  )
}
