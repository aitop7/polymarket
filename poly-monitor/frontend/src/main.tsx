import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import App from './App'
import BacktestPage from './pages/BacktestPage'
import MarketPage from './pages/MarketPage'
import PaperPage from './pages/PaperPage'
import WalletPage from './pages/WalletPage'
import StrategyPage from './pages/StrategyPage'
import PmDataPage from './pages/PmDataPage'
import PredictionPage from './pages/PredictionPage'
import './index.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route element={<App />}>
          {/* One MarketPage instance for `/` and `/:marketId` so Live↔History does not remount. */}
          <Route element={<MarketPage mode="monitor" />}>
            <Route index element={null} />
            <Route path=":marketId" element={null} />
          </Route>
          <Route path="paper" element={<PaperPage />} />
          <Route path="backtest" element={<BacktestPage />} />
          <Route path="wallet" element={<WalletPage />} />
          <Route path="wallet/:walletAddress" element={<WalletPage />} />
          <Route path="pmdata" element={<PmDataPage />} />
          <Route path="strategy" element={<StrategyPage />} />
          <Route path="prediction" element={<PredictionPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </StrictMode>,
)
