import React, { createContext, useContext, useState, useEffect } from 'react'
import { authLogin, authMe } from '../api/index'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const token = localStorage.getItem('token')
    if (token) {
      authMe()
        .then(data => setUser(data))
        .catch(() => {
          localStorage.removeItem('token')
          setUser(null)
        })
        .finally(() => setLoading(false))
    } else {
      setLoading(false)
    }
  }, [])

  const login = async (username, password) => {
    const data = await authLogin(username, password)
    localStorage.setItem('token', data.access_token)
    setUser(data.user)
    return data
  }

  const completePasswordChange = () => {
    setUser(u => (u ? { ...u, must_change_password: false } : u))
  }

  const logout = () => {
    localStorage.removeItem('token')
    setUser(null)
  }

  const hasModule = (module) => {
    if (!user) return false
    if (user.is_admin) return true
    return user.permissions?.modules?.[module] ?? false
  }

  const hasField = (field) => {
    if (!user) return false
    if (user.is_admin) return true
    return user.permissions?.fields?.[field] ?? true
  }

  const authorizedStores = () => {
    if (!user) return []
    if (user.is_admin) return []
    return user.permissions?.stores ?? []
  }

  return (
    <AuthContext.Provider value={{ user, loading, login, logout, completePasswordChange, hasModule, hasField, authorizedStores }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  return useContext(AuthContext)
}
