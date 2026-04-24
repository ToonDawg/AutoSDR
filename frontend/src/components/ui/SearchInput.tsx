import { Search } from 'lucide-react';
import { Input } from '@/components/ui/Input';
import { cn } from '@/lib/utils';

interface SearchInputProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  className?: string;
}

/**
 * The "icon inside the input" pattern used across the index pages. Kept
 * controlled — pages own the query string so they can derive filters /
 * counts from it without waiting for a ref to rehydrate.
 */
export function SearchInput({
  value,
  onChange,
  placeholder = 'Search…',
  className,
}: SearchInputProps) {
  return (
    <div className={cn('relative', className)}>
      <Search
        className="h-3.5 w-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-ink-faint"
        strokeWidth={1.5}
      />
      <Input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="pl-9"
      />
    </div>
  );
}
