import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AuthProvider } from './features/auth/AuthContext'
import { LoginPage, RegisterPage } from './features/auth/AuthPages'
import { RequireAuth } from './features/auth/RequireAuth'
import { ConversationPanel, NoConversationSelected } from './features/conversations/ConversationPanel'
import { ConversationsLayout } from './features/conversations/ConversationsLayout'
import { ScheduledPanel } from './features/scheduled/ScheduledPanel'
import { MyProfilePage, PublicProfilePage } from './features/users/ProfilePages'

function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <main>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/register" element={<RegisterPage />} />
            <Route
              element={
                <RequireAuth>
                  <ConversationsLayout />
                </RequireAuth>
              }
            >
              <Route index element={<NoConversationSelected />} />
              <Route path="c/:conversationId" element={<ConversationPanel />} />
              <Route path="scheduled" element={<ScheduledPanel />} />
              <Route path="me" element={<MyProfilePage />} />
              <Route path="users/:userId" element={<PublicProfilePage />} />
            </Route>
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </AuthProvider>
    </BrowserRouter>
  )
}

export default App
