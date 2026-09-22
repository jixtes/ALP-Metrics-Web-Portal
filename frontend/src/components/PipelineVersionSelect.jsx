export default function PipelineVersionSelect({ id, value, onChange, disabled }) {
  return (
    <>
      <label htmlFor={id}>Pipeline</label>
      <select id={id} value={value} onChange={(event) => onChange(event.target.value)} disabled={disabled}>
        <option value="V3">Version 3 - Latest</option>
        <option value="V2">Version 2</option>
      </select>
    </>
  );
}
