import { useAuth } from '../auth/AuthContext'

// M02 placeholder: proves the session works. Conversations replace it from M04 on.
export function HomePage() {
  const auth = useAuth()
  if (auth.status !== 'signed-in') return null

  return (
    <section>
      <h1>Chat App</h1>
      <p>
        Signed in as <strong data-testid="current-user">{auth.user.display_name}</strong> (@
        {auth.user.username})
      </p>
      <button type="button" onClick={() => void auth.logout()}>
        Sign out
      </button>
    </section>
  )
}
