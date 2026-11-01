export function resolveExamCodeAck(localCode, acknowledgedCode, serverCode) {
  const applied = localCode === undefined || localCode === acknowledgedCode;
  return { applied, content: applied ? serverCode : localCode };
}
