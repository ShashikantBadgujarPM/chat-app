import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { UserSearch } from '../users/UserSearch'

// Placeholder home: session, profile link and user search. Conversations replace it (M04).
export function HomePage() {
  const auth = useAuth()
  const navigate = useNavigate()
  if (auth.status !== 'signed-in') return null

  return (
    <section>
      <h1>Chat App</h1>
      <p>
        Signed in as <strong data-testid="current-user">{auth.user.display_name}</strong> (@
        {auth.user.username}) · <Link to="/me">Edit profile</Link>
      </p>
      <UserSearch label="Find people" onSelect={(user) => navigate(`/users/${user.id}`)} />
      <button type="button" onClick={() => void auth.logout()}>
        Sign out
      </button>
    </section>
  )
}
