export default function PipelineVersionSelect({ id, value, onChange, disabled }) {
  return (
    <>
      <label htmlFor={id}>Pipeline version</label>
      <select id={id} value={value} onChange={(event) => onChange(event.target.value)} disabled={disabled}>
        <option value="V2">V2</option>
        <option value="V3">V3</option>
      </select>
    </>
  );
}
