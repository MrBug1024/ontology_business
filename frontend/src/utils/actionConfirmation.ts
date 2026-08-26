type JsonObject = Record<string, unknown>

function objectValue(value: unknown): JsonObject | null {
  if (typeof value === 'string') {
    try {
      return objectValue(JSON.parse(value))
    } catch {
      return null
    }
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  return value as JsonObject
}

export function actionConfirmationParams(toolCall: unknown, previewPlan: unknown): JsonObject | null {
  const call = objectValue(toolCall)
  const args = objectValue(call?.args ?? call?.arguments)
  const originalParams = objectValue(args?.params)
  if (originalParams) return originalParams

  const plan = objectValue(previewPlan)
  if (plan?.parameters_omitted) return null
  return objectValue(plan?.parameters)
}
