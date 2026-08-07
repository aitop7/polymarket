import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import App from './App'
import BacktestPage from './pages/BacktestPage'
import MarketPage from './pages/MarketPage'
import PaperPage from './pages/PaperPage'
import './index.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route element={<App />}>
          <Route index element={<MarketPage mode="monitor" />} />
          <Route path="paper" element={<PaperPage />} />
          <Route path="backtest" element={<BacktestPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </StrictMode>,
)
