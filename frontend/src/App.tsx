import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AuthProvider } from './features/auth/AuthContext'
import { LoginPage, RegisterPage } from './features/auth/AuthPages'
import { RequireAuth } from './features/auth/RequireAuth'
import { HomePage } from './features/home/HomePage'

function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <main>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/register" element={<RegisterPage />} />
            <Route
              path="/"
              element={
                <RequireAuth>
                  <HomePage />
                </RequireAuth>
              }
            />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </AuthProvider>
    </BrowserRouter>
  )
}

export default App
